"""Protect exact import references and audit bounded source retirement."""

import shutil
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.db import connection
from django.utils import timezone

from counties.common.candidate_staging import drop_candidate_schema
from counties.common.county_registry import registration_for
from counties.common.import_audit import audited_operation
from counties.common.import_states import (
    RETIRED_CANDIDATE_STATES,
    CandidateState,
    OperationStatus,
)
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


def record_publication(candidate, operation):
    """Called inside the county's observed publication transaction."""
    now = timezone.now()
    for previous in (
        ImportCandidate.objects.select_for_update()
        .filter(county=candidate.county, state=CandidateState.PUBLISHED)
        .exclude(pk=candidate.pk)
    ):
        previous.state = CandidateState.SUPERSEDED
        previous.superseded_at = now
        previous.save(update_fields=["state", "superseded_at"])
        drop_candidate_schema(previous)
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
    candidate.state = CandidateState.PUBLISHED
    candidate.published_at = now
    candidate.save(update_fields=["state", "published_at"])
    drop_candidate_schema(candidate)  # cut over, so published data no longer needs it


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
        for candidate in ImportCandidate.objects.filter(
            county=county, state=CandidateState.PUBLISHED
        )
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
        if candidate.state == CandidateState.SUPERSEDED
        else candidate.rejected_at if candidate.state == CandidateState.REJECTED else None
    )
    return retired_at is not None and retired_at + timedelta(days=90) <= now


def _owns_no_retained_candidate(operation, owned, now):
    candidate = owned.get(operation.pk)
    return candidate is None or _eligible(candidate, now)


def _managed_path(candidate, path):
    for source_root in registration_for(candidate.county).source_roots():
        root = source_root.resolve()
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


def _working_directory(operation, path):
    return any(
        path == source_root.resolve() / ".imports" / str(operation.pk)
        for source_root in registration_for(operation.county).source_roots()
    )


@contextmanager
def _audited_removal(owner, kind, key, *, cleanup, actor, reason):
    """Persist intent before any irreversible filesystem action, then record how it ended."""
    with fenced_write():
        audit = ImportAuditEntry.objects.create(
            operation=owner,
            kind=kind,
            actor=actor,
            reason=reason,
            result="running",
            evidence={"cleanup_operation_id": str(cleanup.pk), key: []},
        )
    results = []
    yield audit, results
    with fenced_write():
        audit.result = (
            "completed_with_warnings"
            if any(item["result"] == "failed" for item in results)
            else "completed"
        )
        audit.save(update_fields=["result"])


def cleanup_sources(county, *, actor, reason, origin="command"):
    if not reason.strip():
        raise ValueError("A source cleanup reason is required")
    with audited_operation(county, "source_cleanup", actor=actor, origin=origin) as operation:
        with fenced_write():
            now = timezone.now()
            candidates = list(ImportCandidate.objects.select_for_update().filter(county=county))
            eligible = [candidate for candidate in candidates if _eligible(candidate, now)]
            # Candidates retired by earlier releases left their staged schemas behind.
            retired = {
                candidate.storage_schema: candidate
                for candidate in candidates
                if candidate.state in RETIRED_CANDIDATE_STATES
            }
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s) ORDER BY nspname",
                    [list(retired)],
                )
                dropped_schemas = [schema for (schema,) in cursor.fetchall()]
            for schema in dropped_schemas:
                drop_candidate_schema(retired[schema])
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
            owned = {candidate.operation_id: candidate for candidate in candidates}
            stale = [
                finished
                for finished in ImportOperation.objects.filter(
                    county=county, finished_at__lte=now - timedelta(days=90)
                ).exclude(status=OperationStatus.RUNNING)
                if finished.evidence.get("working_sources")
                and _owns_no_retained_candidate(finished, owned, now)
            ]
        operation.evidence["retired_candidates"] = [str(item.pk) for item in eligible]
        operation.evidence["stale_operations"] = [str(item.pk) for item in stale]
        operation.evidence["dropped_schemas"] = dropped_schemas
        operation.evidence["reason"] = reason.strip()
        operation.save(update_fields=["evidence"])
        audited = {"cleanup": operation, "actor": actor, "reason": reason.strip()}
        for candidate in eligible:
            with _audited_removal(candidate.operation, "source_cleanup", "sources", **audited) as (
                audit,
                results,
            ):
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
                                operation.warnings.append(
                                    f"Source cleanup failed for {path}: {exc}"
                                )
                        results.append({**source, "result": state})
                        audit.evidence["sources"] = results
                        audit.save(update_fields=["evidence"])
                        operation.save(update_fields=["warnings"])
        for finished in stale:
            with _audited_removal(
                finished, "working_source_cleanup", "working_sources", **audited
            ) as (audit, results):
                for recorded in finished.evidence["working_sources"]:
                    with fenced_write():
                        path = Path(recorded).resolve()
                        if not _working_directory(finished, path):
                            state = "unmanaged"
                        elif any(item.is_relative_to(path) for item in protected):
                            state = "protected"
                        elif not path.is_dir():
                            state = "unavailable"
                        else:
                            try:
                                shutil.rmtree(path)
                                state = "removed"
                            except OSError as exc:
                                state = "failed"
                                operation.warnings.append(
                                    f"Source cleanup failed for {path}: {exc}"
                                )
                        results.append({"path": recorded, "result": state})
                        audit.evidence["working_sources"] = results
                        audit.save(update_fields=["evidence"])
                        operation.save(update_fields=["warnings"])
    return operation
