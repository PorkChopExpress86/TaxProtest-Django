"""Coverage review rejects stale evidence identically through every county candidate port."""

from pathlib import Path

from django.db import connection
from django.test import TransactionTestCase

from counties.common.import_review import ImportReviewRejected, checked_binding
from counties.common.models import ImportAuditEntry
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)


class ReviewContract:
    """Behaviour every county port must give coverage review."""

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
        self.add_published_record("CHANGED")
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


class HarrisReviewContractTests(ReviewContract, HarrisCandidateContract, TransactionTestCase):
    pass


class BrazosReviewContractTests(ReviewContract, BrazosCandidateContract, TransactionTestCase):
    pass
