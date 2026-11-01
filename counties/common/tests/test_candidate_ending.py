"""How a candidate's Import operation ended is one typed outcome for every county (ADR-0018)."""

import uuid

from django.test import TransactionTestCase

from counties.common.candidate_lifecycle import apply_candidate, candidate_operation
from counties.common.import_audit import audited_operation
from counties.common.import_states import CandidateState, OperationStatus
from counties.common.models import ImportCandidate, ImportOperation
from counties.common.tests.fake_county import FAKE_COUNTY, registered_fake_county


def candidate_in(state: str) -> ImportCandidate:
    operation = ImportOperation.objects.create(county=FAKE_COUNTY, intent="full")
    return ImportCandidate.objects.create(
        county=FAKE_COUNTY,
        operation=operation,
        state=state,
        storage_schema=f"{FAKE_COUNTY}_candidate_{uuid.uuid4().hex}",
        baseline={},
        request={},
        evidence={},
    )


class CandidateEndingTests(TransactionTestCase):
    def setUp(self):
        self.enterContext(registered_fake_county())

    def test_repeating_a_publication_is_a_typed_already_applied_outcome(self):
        candidate = candidate_in(CandidateState.PUBLISHED)

        with candidate_operation(FAKE_COUNTY, "apply", actor="operator") as handle:
            applied = apply_candidate(handle.import_operation, candidate.pk, reason="Apply again")

        operation = ImportOperation.objects.get(pk=handle.import_operation.pk)
        self.assertEqual(operation.status, OperationStatus.ALREADY_APPLIED)
        self.assertEqual(operation.evidence["already_applied"], str(candidate.pk))
        self.assertTrue(applied.already_applied)
        self.assertFalse(applied.wrote_data)
        self.assertEqual(applied.operation_status, OperationStatus.ALREADY_APPLIED)
        self.assertEqual(applied.state, CandidateState.PUBLISHED)
        self.assertEqual(handle.ending.candidate_id, candidate.pk)
        self.assertEqual(handle.ending.state, CandidateState.PUBLISHED)
        self.assertEqual(handle.ending.warnings, ())

    def test_a_failure_after_publication_is_an_ending_warning_and_not_raised(self):
        candidate = candidate_in(CandidateState.PUBLISHED)

        with candidate_operation(FAKE_COUNTY, "apply", actor="operator") as handle:
            operation = handle.import_operation
            apply_candidate(operation, candidate.pk, reason="Apply")
            # Publication committed (recorded PUBLISHED); a later failure is suppressed.
            operation.status = OperationStatus.PUBLISHED
            operation.save()
            raise OSError("Readiness refresh failed")

        operation.refresh_from_db()
        self.assertEqual(operation.status, OperationStatus.PUBLISHED)
        self.assertEqual(operation.warnings, ["Readiness refresh failed"])
        self.assertEqual(operation.evidence["post_publication_failure"], "Readiness refresh failed")
        self.assertEqual(handle.ending.candidate_id, candidate.pk)
        self.assertEqual(handle.ending.state, CandidateState.PUBLISHED)
        self.assertEqual(handle.ending.warnings, ("Readiness refresh failed",))

    def test_an_operation_without_a_candidate_has_no_candidate_outcome(self):
        with candidate_operation(FAKE_COUNTY, "preview", actor="operator") as handle:
            pass

        self.assertIsNone(handle.ending.candidate_id)
        self.assertIsNone(handle.ending.state)
        self.assertEqual(handle.ending.warnings, ())

    def test_an_unpublished_failure_is_raised_and_leaves_no_ending(self):
        with self.assertRaises(OSError):
            with candidate_operation(FAKE_COUNTY, "full", actor="operator") as handle:
                raise OSError("Source download failed")

        with self.assertRaises(RuntimeError):
            _ = handle.ending


class ExplicitSuppressionTests(TransactionTestCase):
    def setUp(self):
        self.enterContext(registered_fake_county())

    def test_suppression_names_the_failure_on_the_operation(self):
        with audited_operation(FAKE_COUNTY, "apply") as operation:
            operation.status = OperationStatus.PUBLISHED
            operation.publication_after = {"snapshot_id": 7}
            operation.save()
            raise OSError("Extracted files could not be removed")

        self.assertEqual(operation.post_publication_failure, "Extracted files could not be removed")

    def test_an_operation_that_completes_names_no_failure(self):
        with audited_operation(FAKE_COUNTY, "apply") as operation:
            pass

        self.assertIsNone(operation.post_publication_failure)
