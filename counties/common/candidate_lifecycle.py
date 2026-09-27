"""The shared Candidate lifecycle: prepare, qualify, review, publish, and recover.

Every entry point runs inside an Import operation. County behaviour is reached only
through the county candidate port registered for ``operation.county`` (ADR-0018).
"""

from __future__ import annotations

from counties.common.candidate_ports import port_for, published_identity
from counties.common.candidate_staging import cutover_staged_tables
from counties.common.import_audit import OperationStatus
from counties.common.import_retention import record_publication
from counties.common.import_review import authorize_publication
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


def publish(
    operation: ImportOperation, candidate_id, *, user=None, reason: str = ""
) -> ImportCandidate:
    """Publish a qualified candidate atomically under the operation's writer reservation.

    Publication rechecks authorization and exact evidence first; any failure rolls the
    whole cutover and its audit back, leaving the previous dataset published. A
    candidate that is already published is reported as already applied.
    """
    county = operation.county
    tables = port_for(county).tables
    operation.evidence["application_reason"] = reason
    with fenced_write():
        candidate = ImportCandidate.objects.select_for_update().get(pk=candidate_id, county=county)
        if candidate.state in ("published", "superseded"):
            operation.publication_before = operation.publication_after = published_identity(county)
            operation.evidence["already_applied"] = str(candidate.pk)
            operation.status = OperationStatus.ALREADY_APPLIED
            return candidate
        review = authorize_publication(candidate, user=user)
        operation.publication_before = published_identity(county)
        cutover_staged_tables(
            candidate,
            tables.models,
            shared_models_scope={
                model: f" WHERE county = '{county}'" for model in tables.county_scoped
            },
            shared_models_county={model: county for model in tables.county_scoped},
        )
        record_publication(candidate, operation)
        operation.publication_after = {
            **published_identity(county),
            "candidate_id": str(candidate.pk),
        }
        operation.status = OperationStatus.PUBLISHED
        operation.evidence.update(
            candidate_id=str(candidate.pk), qualified_publication="Observed atomic publication"
        )
        operation.save()
        ImportAuditEntry.objects.create(
            operation=candidate.operation,
            kind="publication",
            actor=operation.actor,
            reason=reason or "Qualified candidate applied",
            evidence={
                "before": operation.publication_before,
                "after": operation.publication_after,
                "review_id": str(review.pk) if review else None,
                "operation_id": str(operation.pk),
                "source_years": candidate.sources,
                "county": candidate.evidence.get("audit", {}),
            },
            result="published",
        )
    return candidate
