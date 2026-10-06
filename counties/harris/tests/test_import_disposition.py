"""The Import disposition of real Harris import runs (ADR-0023)."""

from django.contrib.auth import get_user_model

from counties.common.candidate_lifecycle import apply
from counties.common.import_disposition import ImportDispositionKind, import_disposition
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.tests.test_import_command_results import REUSE, HarrisImportResultTestCase


class HarrisImportDispositionTests(HarrisImportResultTestCase):
    def classify(self, operation):
        return import_disposition(operation, subject="Harris import")

    def test_a_blocked_candidate_is_held_with_the_shared_sentence(self):
        self.write_property_source()
        self.call("etl_pipeline", "run", *REUSE, "--scope", "property-only")
        operation, candidate = self.finished("blocked", "blocked")

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.HELD)
        self.assertEqual(disposition.operation_id, operation.pk)
        self.assertEqual(disposition.candidate_id, candidate.pk)
        self.assertFalse(disposition.incomplete)
        self.assertEqual(
            disposition.notice,
            f"Harris import blocked; published data unchanged. Candidate {candidate.pk}; "
            f"operation {operation.pk}. Review in Django admin /admin/data/importcandidate/.",
        )

    def test_a_best_effort_run_that_loaded_some_sources_is_held_and_incomplete(self):
        self.write_full_sources()
        self.call("etl_pipeline", "run", *REUSE, "--allow-partial")
        operation, candidate = self.finished("partial", "blocked")

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.HELD)
        self.assertEqual(disposition.candidate_id, candidate.pk)
        self.assertTrue(disposition.incomplete)
        self.assertEqual(
            disposition.notice,
            f"Harris import blocked (incomplete); published data unchanged. "
            f"Candidate {candidate.pk}; operation {operation.pk}. "
            "Review in Django admin /admin/data/importcandidate/.",
        )

    def test_a_strict_incomplete_run_failed_and_is_not_an_incomplete_import(self):
        self.write_full_sources()
        self.call_failing(Exception, "Pipeline execution failed", "etl_pipeline", "run", *REUSE)
        operation, candidate = self.finished("failed", "blocked")

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.FAILED)
        self.assertEqual(disposition.candidate_id, candidate.pk)
        self.assertFalse(disposition.incomplete)
        self.assertIsNone(disposition.notice)

    def test_a_qualified_import_is_published(self):
        self.publish_baseline("P100")
        self.write_full_sources()
        self.call("import_all_data", *REUSE, "--skip-contract-validation")
        operation, candidate = self.finished("published", "published")

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.PUBLISHED)
        self.assertEqual(disposition.candidate_id, candidate.pk)
        self.assertIsNone(disposition.notice)

    def test_a_preview_is_previewed_without_a_candidate(self):
        self.write_property_source()
        self.call("etl_pipeline", "run", *REUSE, "--scope", "property-only", "--dry-run")
        operation = ImportOperation.objects.get()
        self.assertEqual((operation.intent, operation.status), ("preview", "completed"))

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.PREVIEWED)
        self.assertIsNone(disposition.candidate_id)
        self.assertIsNone(disposition.notice)

    def test_reapplying_a_superseded_candidate_is_already_applied_with_no_new_write(self):
        superseded = ImportCandidate.objects.create(
            county="harris",
            operation=ImportOperation.objects.create(county="harris", intent="full"),
            state="superseded",
            storage_schema="harris_candidate_superseded",
        )
        operator = get_user_model().objects.create_user("operator")

        operation = apply(superseded, user=operator, reason="Re-apply")
        operation.refresh_from_db()
        self.assertEqual(operation.status, "already_applied")

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.ALREADY_APPLIED)
        self.assertEqual(disposition.candidate_id, superseded.pk)
        self.assertEqual(
            disposition.notice,
            f"Harris import already applied earlier; no new write. "
            f"Candidate {superseded.pk}; operation {operation.pk}.",
        )

    def test_classification_leaves_the_persisted_row_untouched(self):
        self.write_property_source()
        self.call("etl_pipeline", "run", *REUSE, "--scope", "property-only")
        before = ImportOperation.objects.values().get()

        self.classify(ImportOperation.objects.get())

        self.assertEqual(ImportOperation.objects.values().get(), before)
        self.assertEqual(before["evidence"]["qualified_publication"], "Published data unchanged")
