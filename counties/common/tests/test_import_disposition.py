"""The Import disposition read over a finished Import operation row (ADR-0023).

Rows here are built directly to pin what the classification never does: raise after a
successful publication, classify an unfinished operation, or change the row. Real runs
of each county are classified in that county's own tests.
"""

from uuid import UUID

from django.test import TestCase

from counties.common.import_disposition import ImportDispositionKind, import_disposition
from counties.common.models import ImportOperation

CANDIDATE = UUID("11111111-2222-3333-4444-555555555555")


class PublishedDispositionTests(TestCase):
    def test_a_publication_with_a_later_failure_is_still_published(self):
        operation = ImportOperation.objects.create(
            county="harris",
            intent="full",
            status="published",
            evidence={
                "candidate_id": str(CANDIDATE),
                "post_publication_failure": "Readiness refresh failed",
            },
            warnings=["Readiness refresh failed"],
        )

        disposition = import_disposition(operation, subject="Harris import")

        self.assertIs(disposition.kind, ImportDispositionKind.PUBLISHED)
        self.assertEqual(disposition.operation_id, operation.pk)
        self.assertEqual(disposition.candidate_id, CANDIDATE)
        self.assertFalse(disposition.incomplete)
        self.assertIsNone(disposition.notice)

    def test_a_publication_never_raises_over_unexpected_evidence(self):
        for evidence in ({}, {"candidate_id": "not-a-uuid"}, {"candidate_id": None}):
            with self.subTest(evidence=evidence):
                operation = ImportOperation.objects.create(
                    county="brazos", intent="annual", status="published", evidence=evidence
                )

                disposition = import_disposition(operation, subject="Brazos annual import")

                self.assertIs(disposition.kind, ImportDispositionKind.PUBLISHED)
                self.assertIsNone(disposition.candidate_id)


class UnsettledOperationTests(TestCase):
    def test_an_unfinished_or_non_import_operation_has_no_disposition(self):
        for intent, status in (
            ("full", "running"),
            ("cad_source_staging", "completed"),
            ("annual", "validated"),
        ):
            with self.subTest(intent=intent, status=status):
                operation = ImportOperation.objects.create(
                    county="brazos", intent=intent, status=status
                )

                with self.assertRaises(ValueError):
                    import_disposition(operation, subject="Brazos import")


class PreviewDispositionTests(TestCase):
    def test_a_best_effort_preview_after_a_failed_fetch_is_previewed_and_incomplete(self):
        # A best-effort preview whose download stage failed still validates the sources
        # it has, so it ends partial without ever preparing a candidate.
        operation = ImportOperation.objects.create(
            county="harris", intent="preview", status="partial", evidence={"sources": []}
        )

        disposition = import_disposition(operation, subject="Harris import")

        self.assertIs(disposition.kind, ImportDispositionKind.PREVIEWED)
        self.assertIsNone(disposition.candidate_id)
        self.assertTrue(disposition.incomplete)
        self.assertIsNone(disposition.notice)

    def test_a_failed_preview_is_failed(self):
        operation = ImportOperation.objects.create(
            county="harris", intent="preview", status="failed"
        )

        disposition = import_disposition(operation, subject="Harris import")

        self.assertIs(disposition.kind, ImportDispositionKind.FAILED)
        self.assertIsNone(disposition.candidate_id)
