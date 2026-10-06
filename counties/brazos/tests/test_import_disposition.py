"""The Import disposition of real Brazos import runs (ADR-0023)."""

from django.contrib.auth import get_user_model

from counties.brazos.tests.test_import_command_results import (
    ACCOUNTS,
    OFFLINE_2026,
    BrazosImportResultTestCase,
)
from counties.brazos.tests.test_property_coverage import write_gis, write_pacs
from counties.common.candidate_lifecycle import apply
from counties.common.import_disposition import ImportDispositionKind, import_disposition
from counties.common.models import ImportCandidate, ImportOperation


class BrazosImportDispositionTests(BrazosImportResultTestCase):
    def classify(self, operation):
        return import_disposition(operation, subject="Brazos annual import")

    def test_a_first_import_is_held_for_review_with_the_shared_sentence(self):
        write_pacs(self.root, ACCOUNTS, equity=True)
        write_gis(self.root, ACCOUNTS)
        self.call("refresh_brazos_annual", *OFFLINE_2026)
        operation, candidate = self.finished("awaiting_review", "awaiting_review")

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.HELD)
        self.assertEqual(disposition.candidate_id, candidate.pk)
        self.assertFalse(disposition.incomplete)
        self.assertEqual(
            disposition.notice,
            f"Brazos annual import awaiting_review; published data unchanged. "
            f"Candidate {candidate.pk}; operation {operation.pk}. "
            "Review in Django admin /admin/data/importcandidate/.",
        )

    def test_a_blocked_candidate_is_held(self):
        write_pacs(self.root, ACCOUNTS, owners=False)
        self.call("load_brazos_cad", *OFFLINE_2026)
        operation, candidate = self.finished("blocked", "blocked")

        disposition = import_disposition(operation, subject="Brazos CAD import")

        self.assertIs(disposition.kind, ImportDispositionKind.HELD)
        self.assertEqual(
            disposition.notice,
            f"Brazos CAD import blocked; published data unchanged. "
            f"Candidate {candidate.pk}; operation {operation.pk}. "
            "Review in Django admin /admin/data/importcandidate/.",
        )

    def test_an_incomplete_source_failed(self):
        source = self.root / "extracted" / "2026"
        source.mkdir(parents=True)
        (source / "APPRAISAL_INFO.TXT").write_text("incomplete", encoding="utf-8")
        self.call_failing("load_brazos_cad", *OFFLINE_2026)
        operation, candidate = self.finished("failed", "blocked")

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.FAILED)
        self.assertEqual(disposition.candidate_id, candidate.pk)
        self.assertFalse(disposition.incomplete)
        self.assertIsNone(disposition.notice)

    def test_a_qualified_refresh_is_published(self):
        self.publish_completed_2025_snapshot()
        write_pacs(self.root, ACCOUNTS, equity=True)
        write_gis(self.root, ACCOUNTS)
        self.call("refresh_brazos_annual", *OFFLINE_2026)
        operation, candidate = self.finished("published", "published")

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.PUBLISHED)
        self.assertEqual(disposition.candidate_id, candidate.pk)
        self.assertIsNone(disposition.notice)

    def test_a_dry_run_is_previewed_without_a_candidate(self):
        write_pacs(self.root, ACCOUNTS, equity=True)
        write_gis(self.root, ACCOUNTS)
        self.call("refresh_brazos_annual", *OFFLINE_2026, "--dry-run")
        operation = ImportOperation.objects.get()
        self.assertEqual((operation.intent, operation.status), ("preview", "completed"))

        disposition = self.classify(operation)

        self.assertIs(disposition.kind, ImportDispositionKind.PREVIEWED)
        self.assertIsNone(disposition.candidate_id)
        self.assertIsNone(disposition.notice)

    def test_reapplying_a_superseded_candidate_is_already_applied_with_no_new_write(self):
        superseded = ImportCandidate.objects.create(
            county="brazos",
            operation=ImportOperation.objects.create(county="brazos", intent="annual"),
            state="superseded",
            storage_schema="brazos_candidate_superseded",
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
            f"Brazos annual import already applied earlier; no new write. "
            f"Candidate {superseded.pk}; operation {operation.pk}.",
        )

    def test_classification_leaves_the_persisted_row_untouched(self):
        write_pacs(self.root, ACCOUNTS, equity=True)
        write_gis(self.root, ACCOUNTS)
        self.call("refresh_brazos_annual", *OFFLINE_2026)
        before = ImportOperation.objects.values().get()

        self.classify(ImportOperation.objects.get())

        self.assertEqual(ImportOperation.objects.values().get(), before)
        self.assertEqual(before["evidence"]["qualified_publication"], "Published data unchanged")
