"""Bind human coverage decisions to exact county-owned candidate evidence."""

import hashlib
import json
from pathlib import Path

from django.core.exceptions import PermissionDenied
from django.utils import timezone

from counties.common.import_audit import audited_operation
from counties.common.import_coverage import compare_coverage
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate


class ImportReviewRejected(ValueError):
    pass


def captured_binding(candidate) -> str:
    payload = {
        "candidate": str(candidate.pk),
        "state": candidate.state,
        "request": candidate.request,
        "sources": candidate.sources,
        "baseline": candidate.baseline,
        "evidence": candidate.evidence,
        "validation": candidate.operation.evidence,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def county_identities(candidate):
    if candidate.county == "harris":
        from counties.harris.etl_pipeline.candidate import dataset_identity, published_identity
    elif candidate.county == "brazos":
        from counties.brazos.property_candidate import dataset_identity, published_identity
    else:
        raise ImportReviewRejected("Unknown county candidate")
    return published_identity(), dataset_identity(candidate.storage_schema)


def current_coverage(candidate):
    if candidate.county == "harris":
        from counties.harris.adapter import adapter
        from counties.harris.etl_pipeline.candidate import candidate_tables
        from counties.harris.etl_pipeline.coverage import outcome_populations
        from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
        from counties.harris.source_catalog import HarrisImportStage

        previous = outcome_populations(adapter.published_year())
        with candidate_tables(candidate):
            current = outcome_populations(
                candidate.evidence.get("property_source_year"),
                claimed_gis=HarrisImportStage.GIS
                in HarrisImportPlan.from_legacy_scope(candidate.request["plan"]).stages,
            )
    else:
        from counties.brazos.property_candidate import candidate_tables
        from counties.brazos.property_coverage import outcome_populations

        previous = outcome_populations()
        with candidate_tables(candidate):
            partial = candidate.request["mode"] == "cad_recovery"
            current = outcome_populations(claimed_gis=not partial, deliberately_absent_gis=partial)
    return compare_coverage(previous, current)


def checked_binding(candidate: ImportCandidate) -> str:
    operation = candidate.operation
    validation = operation.evidence.get(
        "validation" if candidate.county == "harris" else "source_validation", {}
    )
    if not validation.get("valid"):
        raise ImportReviewRejected("Source integrity qualification failed; prepare fresh evidence")
    coverage = candidate.evidence.get("coverage")
    if not coverage:
        raise ImportReviewRejected("Candidate coverage qualification unavailable")
    if coverage["hard_failures"]:
        raise ImportReviewRejected("; ".join(coverage["hard_failures"]))
    if not candidate.sources:
        raise ImportReviewRejected("Exact candidate source evidence unavailable")
    for source in candidate.sources:
        path = Path(source["path"])
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise ImportReviewRejected(
                "Retained source unavailable; prepare fresh evidence"
            ) from exc
        if digest.hexdigest() != source["sha256"]:
            raise ImportReviewRejected("Retained source changed; prepare fresh evidence")
    baseline, content = county_identities(candidate)
    if baseline != candidate.baseline:
        raise ImportReviewRejected("Published baseline changed; prepare fresh comparison evidence")
    if not candidate.evidence.get("content_identity"):
        raise ImportReviewRejected(
            "Candidate content identity not recorded; prepare fresh evidence"
        )
    if content != candidate.evidence["content_identity"]:
        raise ImportReviewRejected(
            "Candidate contents changed; prepare fresh qualification evidence"
        )
    if current_coverage(candidate) != coverage:
        raise ImportReviewRejected(
            "Outcome qualification changed; prepare fresh evidence and review"
        )
    payload = {
        "candidate": str(candidate.pk),
        "request": candidate.request,
        "sources": candidate.sources,
        "baseline": baseline,
        "content": content,
        "coverage": coverage,
        "validation": validation,
        "property_source_year": candidate.evidence.get("property_source_year"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def authorize_publication(candidate, *, user=None):
    binding = checked_binding(candidate)
    if candidate.state in ("blocked", "rejected", "preparing"):
        raise ImportReviewRejected("Candidate is not qualified for publication")
    if not candidate.evidence["coverage"]["requires_review"]:
        return None
    from django.contrib.auth import get_user_model

    review = candidate.operation.audit_entries.filter(
        kind="coverage_review", result="approved"
    ).last()
    if candidate.state != "approved" or review is None:
        raise ImportReviewRejected("Coverage review approval is required before publication")
    reviewer = (
        get_user_model()
        .objects.filter(username=review.actor, is_active=True, is_staff=True)
        .first()
    )
    if reviewer is None or not reviewer.has_perm("data.approve_import_coverage"):
        raise ImportReviewRejected("Reviewer's coverage permission is no longer valid")
    if review.evidence.get("qualification_binding") != binding:
        raise ImportReviewRejected("Approved qualification changed; prepare fresh review evidence")
    if (
        user is None
        or not user.is_active
        or not user.is_staff
        or not user.has_perm("data.approve_import_coverage")
    ):
        raise ImportReviewRejected(
            "An authorized operator must explicitly apply reviewed candidates"
        )
    return review


def review_candidate(candidate, *, user, reason: str, decision: str, expected_binding: str):
    if (
        not user.is_authenticated
        or not user.is_active
        or not user.is_staff
        or not user.has_perm("data.approve_import_coverage")
    ):
        raise PermissionDenied
    if not reason.strip() or decision not in ("approved", "rejected"):
        raise ImportReviewRejected("A valid decision and justification are required")
    evidence = {
        "candidate_id": str(candidate.pk),
        "binding": expected_binding,
        "baseline": candidate.baseline,
        "content_identity": candidate.evidence.get("content_identity"),
        "sources": candidate.sources,
    }
    try:
        with audited_operation(
            candidate.county, "coverage_review", actor=user.get_username(), origin="admin"
        ):
            with fenced_write():
                candidate = ImportCandidate.objects.select_for_update().get(pk=candidate.pk)
                if candidate.state in ("approved", "rejected", "published", "superseded"):
                    raise ImportReviewRejected(
                        "Candidate already has a review decision or publication"
                    )
                if captured_binding(candidate) != expected_binding:
                    raise ImportReviewRejected(
                        "Review evidence changed; reload and review fresh evidence"
                    )
                if decision == "approved":
                    evidence["qualification_binding"] = checked_binding(candidate)
                ImportAuditEntry.objects.create(
                    operation=candidate.operation,
                    kind="coverage_review",
                    actor=user.get_username(),
                    reason=reason.strip(),
                    evidence=evidence,
                    result=decision,
                )
                candidate.state = decision
                if decision == "rejected":
                    candidate.rejected_at = timezone.now()
                candidate.save(update_fields=["state", "rejected_at"])
    except Exception as exc:
        ImportAuditEntry.objects.create(
            operation=candidate.operation,
            kind="coverage_review",
            actor=user.get_username(),
            reason=reason.strip(),
            evidence={**evidence, "rejection": str(exc)},
            result="rejected",
        )
        raise ImportReviewRejected(str(exc)) from exc
