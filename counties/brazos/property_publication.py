"""Atomic publication of qualified Brazos candidate facts and active snapshot."""

from counties.brazos.models import BrazosPropertySnapshot, SnapshotOutcome
from counties.brazos.property_candidate import MODELS, published_identity
from counties.common.candidate_staging import cutover_staged_tables
from counties.common.import_retention import record_publication
from counties.common.import_review import ImportReviewRejected, authorize_publication
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate
from counties.common.tax_models import PropertyJurisdictionExemption


def publish_candidate(candidate_id, operation, *, user=None):
    with fenced_write():
        candidate = ImportCandidate.objects.select_for_update().get(
            pk=candidate_id, county="brazos"
        )
        if candidate.state in ("published", "superseded"):
            operation.publication_before = operation.publication_after = published_identity()
            operation.evidence["already_applied"] = str(candidate.pk)
            return candidate
        review = authorize_publication(candidate, user=user)
        active = BrazosPropertySnapshot.objects.filter(is_active=True).first()
        if candidate.request["mode"] == "gis_recovery" and (
            active is None
            or active.pk != candidate.baseline["snapshot_id"]
            or active.outcome != SnapshotOutcome.PARTIAL
            or active.tax_year != candidate.request["tax_year"]
        ):
            raise ImportReviewRejected(
                "GIS recovery no longer targets the recorded active Partial snapshot"
            )
        operation.publication_before = published_identity()
        cutover_staged_tables(
            candidate,
            MODELS,
            shared_models_scope={PropertyJurisdictionExemption: " WHERE county = 'brazos'"},
            shared_models_county={PropertyJurisdictionExemption: "brazos"},
        )
        record_publication(candidate, operation)
        operation.publication_after = {**published_identity(), "candidate_id": str(candidate.pk)}
        operation.status = "published"
        operation.evidence.update(
            candidate_id=str(candidate.pk), qualified_publication="Observed atomic publication"
        )
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
                "source_years": candidate.sources,
                "capabilities": candidate.evidence["capabilities"],
            },
            result="published",
        )
    return candidate
