"""Atomic publication of qualified Brazos candidate facts and active snapshot."""

from django.db import connection

from counties.brazos.models import BrazosPropertySnapshot, SnapshotOutcome
from counties.brazos.property_candidate import MODELS, published_identity
from counties.common.import_review import ImportReviewRejected, authorize_publication
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate
from counties.common.tax_models import PropertyJurisdictionExemption


def publish_candidate(candidate_id, operation, *, user=None):
    with fenced_write():
        candidate = ImportCandidate.objects.select_for_update().get(
            pk=candidate_id, county="brazos"
        )
        if candidate.state == "published":
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
        with connection.cursor() as cursor:
            for model in reversed(MODELS):
                scope = " WHERE county = 'brazos'" if model is PropertyJurisdictionExemption else ""
                cursor.execute(
                    f"DELETE FROM public.{connection.ops.quote_name(model._meta.db_table)}{scope}"
                )
            for model in MODELS:
                table = connection.ops.quote_name(model._meta.db_table)
                if model is PropertyJurisdictionExemption:
                    # County-private candidate sequences must not collide with
                    # another county's IDs in this shared table.
                    columns = ", ".join(
                        connection.ops.quote_name(field.column)
                        for field in model._meta.fields
                        if field.name != "id"
                    )
                    cursor.execute(
                        f'INSERT INTO public.{table} ({columns}) SELECT {columns} FROM "{candidate.storage_schema}".{table} WHERE county = %s',
                        ["brazos"],
                    )
                    continue
                cursor.execute(
                    f'INSERT INTO public.{table} OVERRIDING SYSTEM VALUE SELECT * FROM "{candidate.storage_schema}".{table}'
                )
                cursor.execute(
                    "SELECT pg_get_serial_sequence(%s, 'id')", [f"public.{model._meta.db_table}"]
                )
                sequence = cursor.fetchone()[0]
                cursor.execute(f"SELECT last_value FROM {sequence}")
                last_value = cursor.fetchone()[0]
                cursor.execute(f"SELECT COALESCE(MAX(id), 1) FROM public.{table}")
                cursor.execute(
                    "SELECT setval(%s::regclass, %s, true)",
                    [sequence, max(last_value, cursor.fetchone()[0])],
                )
        candidate.state = "published"
        candidate.save(update_fields=["state"])
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
