"""Publish qualified Harris tables with one atomic observed transition."""

from django.db import connection

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
        with connection.cursor() as cursor:
            for model in reversed(MODELS):
                cursor.execute(
                    f"DELETE FROM public.{connection.ops.quote_name(model._meta.db_table)}"
                )
            for model in MODELS:
                table = connection.ops.quote_name(model._meta.db_table)
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
                maximum = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT setval(%s::regclass, %s, true)", [sequence, max(last_value, maximum)]
                )
        record_publication(candidate, operation)
        operation.publication_after = {
            **published_identity(),
            "candidate_id": str(candidate.pk),
            "data_year": candidate.request["data_year"],
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
