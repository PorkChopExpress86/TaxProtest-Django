"""Protect exact import references and audit bounded source retirement."""

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from counties.common.import_audit import audited_operation
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


def record_publication(candidate, operation):
    """Called inside the county's observed publication transaction."""
    now = timezone.now()
    for previous in (
        ImportCandidate.objects.select_for_update()
        .filter(county=candidate.county, state="published")
        .exclude(pk=candidate.pk)
    ):
        previous.state = "superseded"
        previous.superseded_at = now
        previous.save(update_fields=["state", "superseded_at"])
        ImportAuditEntry.objects.create(
            operation=previous.operation,
            kind="supersession",
            actor=operation.actor,
            reason="A newer qualified dataset was published",
            evidence={
                "replacement_candidate_id": str(candidate.pk),
                "operation_id": str(operation.pk),
            },
            result="superseded",
        )
    candidate.state = "published"
    candidate.published_at = now
    candidate.save(update_fields=["state", "published_at"])


def source_availability(sources):
    """Display existence without silently asserting a digest verification."""
    return [
        {
            **source,
            "availability": (
                "Retained (digest recorded)" if Path(source["path"]).is_file() else "Unavailable"
            ),
        }
        for source in sources
    ]


def baseline_sources(county):
    """Keep recorded provenance when a county operation retains baseline facts."""
    return [
        {
            **source,
            "reference": "inherited",
            "source_operation_id": source.get("source_operation_id") or str(candidate.operation_id),
        }
        for candidate in ImportCandidate.objects.filter(county=county, state="published")
        for source in candidate.sources
    ]


def retain_baseline_sources(operation, inherited):
    sources = operation.evidence.setdefault("sources", [])
    existing = {(source["path"], source["sha256"]) for source in sources}
    sources.extend(
        source for source in inherited if (source["path"], source["sha256"]) not in existing
    )


def _eligible(candidate, now):
    retired_at = (
        candidate.superseded_at
        if candidate.state == "superseded"
        else candidate.rejected_at if candidate.state == "rejected" else None
    )
    return retired_at is not None and retired_at + timedelta(days=90) <= now


def _managed_path(candidate, path):
    prefix = "BCAD" if candidate.county == "brazos" else "HCAD"
    for setting in (f"{prefix}_DOWNLOAD_DIR", f"{prefix}_EXTRACT_DIR"):
        root = Path(getattr(settings, setting)).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) > 2 and relative.parts[:2] == (
            ".imports",
            str(candidate.operation_id),
        ):
            return True
    return False


def cleanup_sources(county, *, actor, reason, origin="command"):
    if not reason.strip():
        raise ValueError("A source cleanup reason is required")
    with audited_operation(county, "source_cleanup", actor=actor, origin=origin) as operation:
        with fenced_write():
            now = timezone.now()
            candidates = list(ImportCandidate.objects.select_for_update().filter(county=county))
            eligible = [candidate for candidate in candidates if _eligible(candidate, now)]
            protected = {
                Path(source["path"]).resolve()
                for candidate in ImportCandidate.objects.all()
                if not _eligible(candidate, now)
                for source in candidate.sources
            }
            # Unknown lifecycle and independent history/tax inputs remain retained.
            for other in ImportOperation.objects.filter(candidate__isnull=True):
                protected.update(
                    Path(source["path"]).resolve() for source in other.evidence.get("sources", [])
                )
        operation.evidence["retired_candidates"] = [str(item.pk) for item in eligible]
        operation.evidence["reason"] = reason.strip()
        operation.save(update_fields=["evidence"])
        for candidate in eligible:
            # Persist intent before any irreversible filesystem action.
            with fenced_write():
                audit = ImportAuditEntry.objects.create(
                    operation=candidate.operation,
                    kind="source_cleanup",
                    actor=actor,
                    reason=reason.strip(),
                    result="running",
                    evidence={"cleanup_operation_id": str(operation.pk), "sources": []},
                )
            results = []
            for source in candidate.sources:
                with fenced_write():
                    path = Path(source["path"]).resolve()
                    state = "protected" if path in protected else "unmanaged"
                    if path not in protected and _managed_path(candidate, path):
                        try:
                            if path.is_file():
                                path.unlink()
                                state = "removed"
                            else:
                                state = "unavailable"
                        except OSError as exc:
                            state = "failed"
                            operation.warnings.append(f"Source cleanup failed for {path}: {exc}")
                    results.append({**source, "result": state})
                    audit.evidence["sources"] = results
                    audit.save(update_fields=["evidence"])
                    operation.save(update_fields=["warnings"])
            with fenced_write():
                audit.result = (
                    "completed_with_warnings"
                    if any(item["result"] == "failed" for item in results)
                    else "completed"
                )
                audit.save(update_fields=["result"])
    return operation
