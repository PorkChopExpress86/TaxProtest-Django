"""Candidate state and Import operation status: one shared vocabulary, paired explicitly."""

from django.test import SimpleTestCase

from counties.common.import_states import (
    CandidateState,
    LegacyOperationStatus,
    OperationStatus,
    operation_status_for,
)


class StoredValueTests(SimpleTestCase):
    """Stored values are a persisted contract and stay byte-identical (ADR-0023, ADR-0024)."""

    def test_candidate_state_values_are_the_stored_spellings(self):
        self.assertEqual(
            {state.value for state in CandidateState},
            {
                "preparing",
                "prepared",
                "awaiting_review",
                "blocked",
                "approved",
                "rejected",
                "published",
                "superseded",
            },
        )

    def test_operation_status_values_are_the_stored_spellings(self):
        self.assertEqual(
            {status.value for status in OperationStatus},
            {
                "running",
                "completed",
                "completed_with_warnings",
                "partial",
                "prepared",
                "blocked",
                "awaiting_review",
                "published",
                "already_applied",
                "failed",
            },
        )

    def test_legacy_operation_statuses_are_the_stored_spellings(self):
        self.assertEqual({status.value for status in LegacyOperationStatus}, {"validated"})

    def test_a_legacy_status_is_not_a_current_operation_status(self):
        with self.assertRaises(ValueError):
            OperationStatus("validated")


class PairingTests(SimpleTestCase):
    def test_states_that_can_end_an_operation_pair_with_their_status(self):
        for state, status in (
            ("prepared", "prepared"),
            ("awaiting_review", "awaiting_review"),
            ("blocked", "blocked"),
            ("published", "published"),
        ):
            with self.subTest(state=state):
                paired = operation_status_for(CandidateState(state))

                self.assertIs(type(paired), OperationStatus)
                self.assertEqual(paired.value, status)

    def test_states_that_cannot_end_an_operation_are_not_paired(self):
        for state in ("preparing", "approved", "rejected", "superseded"):
            with self.subTest(state=state), self.assertRaises(KeyError):
                operation_status_for(CandidateState(state))
