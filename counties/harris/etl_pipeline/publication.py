from counties.common.candidate_staging import cutover_staged_tables
from counties.common.import_retention import record_publication
from counties.common.import_review import authorize_publication
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate

from .candidate import MODELS, published_identity


def publish_candidate(candidate_id, operation, *, user=None):
    with fenced_write():
        candidate = ImportCandidate.objects.select_for_update().get(
            pk=candidate_id, county="harris"
        )
        if candidate.state in ("published", "superseded"):
            operation.publication_before = operation.publication_after = published_identity()
            operation.evidence["already_applied"] = str(candidate.pk)
            return candidate
        review = authorize_publication(candidate, user=user)
        operation.publication_before = published_identity()
        cutover_staged_tables(candidate, MODELS, drop_staged=False)
        record_publication(candidate, operation)
        operation.publication_after = {
            **published_identity(),
            "candidate_id": str(candidate.pk),
            "data_year": candidate.request["data_year"],
            "property_source_year": candidate.evidence.get("property_source_year"),
        }
        operation.status = "published"
        operation.evidence["qualified_publication"] = "Observed atomic publication"
        operation.evidence["candidate_id"] = str(candidate.pk)
        operation.save()
        ImportAuditEntry.objects.create(
            operation=candidate.operation,
            kind="publication",
            actor=operation.actor,
            reason=operation.evidence.get("application_reason") or "Qualified candidate applied",
            evidence={
                "before": operation.publication_before,
                "after": operation.publication_after,
                "review_id": str(review.pk) if review else None,
                "operation_id": str(operation.pk),
            },
            result="published",
        )
    return candidate
