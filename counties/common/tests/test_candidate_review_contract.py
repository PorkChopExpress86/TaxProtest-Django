"""Coverage review rejects stale evidence identically through every county candidate port."""

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase

from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.models import PropertyAccount
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
)
from counties.brazos.tests.test_property_coverage import write_pacs
from counties.common.import_review import (
    ImportReviewRejected,
    captured_binding,
    checked_binding,
    review_candidate,
)
from counties.common.models import ImportAuditEntry, ImportCandidate
from counties.harris.etl_pipeline import run_harris_import
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
from counties.harris.etl_pipeline.tests.test_import_publication import harris_request
from counties.harris.models import PropertyRecord


class ReviewContract:
    """Behaviour every county port must give coverage review; subclasses bind one county."""

    candidate_column: tuple[str, str]  # a staged table and one of its text columns

    def prepare(self, root: Path) -> ImportCandidate:
        raise NotImplementedError

    def change_published_data(self):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.candidate = self.prepare(self.root)
        self.assertEqual(self.candidate.state, "awaiting_review")
        self.reviewer = get_user_model().objects.create_superuser("reviewer", password="test")

    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def approve(self):
        return review_candidate(
            self.candidate,
            user=self.reviewer,
            reason="Reviewed exact candidate evidence",
            decision="approved",
            expected_binding=captured_binding(self.candidate),
        )

    def assert_rejected(self, message):
        with self.assertRaisesMessage(ImportReviewRejected, message):
            self.approve()
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.state, "awaiting_review")

    def change_recorded_coverage(self, **changes):
        self.candidate.evidence["coverage"].update(changes)
        self.candidate.save(update_fields=["evidence"])

    def test_valid_candidate_records_its_qualification_binding(self):
        self.approve()
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.state, "approved")
        review = ImportAuditEntry.objects.get(kind="coverage_review", result="approved")
        self.assertEqual(review.evidence["qualification_binding"], checked_binding(self.candidate))

    def test_changed_published_baseline_is_rejected(self):
        self.change_published_data()
        self.assert_rejected("Published baseline changed")

    def test_changed_candidate_contents_are_rejected(self):
        table, column = self.candidate_column
        with connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{self.candidate.storage_schema}"."{table}" SET "{column}" = %s',
                ["Changed after qualification"],
            )
        self.assert_rejected("Candidate contents changed")

    def test_coverage_measured_differently_is_rejected(self):
        outcomes = self.candidate.evidence["coverage"]["outcomes"]
        outcomes["search"]["exclusion_reasons"] = {"SOMEONE": ["Measured by other rules"]}
        self.change_recorded_coverage(outcomes=outcomes)
        self.assert_rejected("Outcome qualification changed")

    def test_changed_retained_source_is_rejected(self):
        Path(self.candidate.sources[0]["path"]).write_text("changed")
        self.assert_rejected("Retained source changed")

    def test_failed_source_validation_is_rejected(self):
        operation = self.candidate.operation
        operation.evidence["validation"]["valid"] = False
        operation.save(update_fields=["evidence"])
        self.assert_rejected("Source integrity qualification failed")

    def test_hard_coverage_failure_is_rejected(self):
        self.change_recorded_coverage(
            hard_failures=["search: zero eligible records for a supported outcome"]
        )
        self.assert_rejected("zero eligible records")


class HarrisReviewContractTests(ReviewContract, TransactionTestCase):
    candidate_column = ("data_propertyrecord", "address")

    def prepare(self, root):
        self.enterContext(self.settings(**_runtime_settings(root)))
        result = run_harris_import(harris_request(root, prepare=True))
        return ImportCandidate.objects.get(pk=result.candidate_id)

    def change_published_data(self):
        PropertyRecord.objects.create(account_number="PUBLISHED")


class BrazosReviewContractTests(ReviewContract, TransactionTestCase):
    candidate_column = ("brazos_cad_propertyaccount", "owner_name")

    def prepare(self, root):
        write_pacs(root, ["000000010013"])
        self.enterContext(
            self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                BCAD_EXTRACT_DIR=str(root / "extracted"),
            )
        )
        result = BrazosPropertyImport(CadRefreshStage(), None).run(
            PropertyImportRequest(
                mode=PropertyImportMode.CAD_RECOVERY,
                options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
                prepare_only=True,
            )
        )
        return ImportCandidate.objects.get(pk=result.candidate_id)

    def change_published_data(self):
        PropertyAccount.objects.create(tax_year=2025, prop_id="PUBLISHED", owner_name="Live")
