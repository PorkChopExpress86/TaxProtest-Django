"""An Import operation whose worker died is failed by the next writer of its county (#133)."""

from uuid import uuid4

from django.test import TransactionTestCase

from counties.common.import_audit import audited_operation, reconcile_dead_operations
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation
from counties.common.tests.test_candidate_schema_retirement import (
    leave_schema_behind,
    schema_exists,
)

DEAD = (
    "Import operation never finished: its worker stopped, and another Import operation "
    "has since held the county writer"
)


def crashed_preparation(county, *, state="preparing", evidence=None):
    """The rows a worker leaves when it dies during preparation, before any cleanup runs."""
    operation = ImportOperation.objects.create(county=county, intent="property-only")
    candidate = ImportCandidate.objects.create(
        county=county,
        operation=operation,
        state=state,
        storage_schema=f"{county}_candidate_{uuid4().hex}",
        evidence=evidence or {},
    )
    leave_schema_behind(candidate)
    return operation, candidate


def next_operation(county):
    with audited_operation(county, "fixture", actor="operator") as operation:
        pass
    return operation


class DeadOperationReconciliationTests(TransactionTestCase):
    def test_next_writer_fails_a_crashed_preparation_and_blocks_its_candidate(self):
        for county in ("harris", "brazos"):
            with self.subTest(county):
                dead, candidate = crashed_preparation(county)
                operation = next_operation(county)
                dead.refresh_from_db()
                candidate.refresh_from_db()
                self.assertEqual((dead.status, dead.errors), ("failed", [DEAD]))
                self.assertIsNotNone(dead.finished_at)
                self.assertEqual(
                    (candidate.state, candidate.evidence), ("blocked", {"error": DEAD})
                )
                self.assertFalse(schema_exists(candidate))
                entry = ImportAuditEntry.objects.get(operation=dead)
                self.assertEqual(
                    (entry.kind, entry.actor, entry.reason, entry.result, entry.evidence),
                    (
                        "operation_reconciliation",
                        "operator",
                        DEAD,
                        "failed",
                        {
                            "reconciled_by": str(operation.pk),
                            "candidate_id": str(candidate.pk),
                            "candidate_state_before": "preparing",
                        },
                    ),
                )
                self.assertEqual(operation.status, "completed")

    def test_reconciliation_repeats_without_further_change(self):
        dead, _ = crashed_preparation("harris")
        next_operation("harris")
        next_operation("harris")
        dead.refresh_from_db()
        self.assertEqual(dead.errors, [DEAD])
        self.assertEqual(ImportAuditEntry.objects.filter(operation=dead).count(), 1)

    def test_a_candidate_blocked_before_the_crash_keeps_its_error(self):
        dead, candidate = crashed_preparation(
            "harris", state="blocked", evidence={"error": "Source unreadable"}
        )
        next_operation("harris")
        dead.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual(dead.status, "failed")
        self.assertEqual(
            (candidate.state, candidate.evidence), ("blocked", {"error": "Source unreadable"})
        )
        self.assertFalse(schema_exists(candidate))
        self.assertEqual(
            ImportAuditEntry.objects.get(operation=dead).evidence["candidate_state_before"],
            "blocked",
        )

    def test_an_operation_without_a_candidate_is_not_proven_to_have_held_the_writer(self):
        waiting = ImportOperation.objects.create(county="harris", intent="property-only")
        next_operation("harris")
        waiting.refresh_from_db()
        self.assertEqual((waiting.status, waiting.errors), ("running", []))
        self.assertFalse(ImportAuditEntry.objects.filter(operation=waiting).exists())

    def test_only_the_acquiring_county_is_reconciled(self):
        dead, candidate = crashed_preparation("brazos")
        next_operation("harris")
        dead.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual((dead.status, candidate.state), ("running", "preparing"))
        self.assertTrue(schema_exists(candidate))

    def test_reconcile_on_request_records_an_operation_only_when_one_is_dead(self):
        reconcile_dead_operations("harris", actor="container start")
        self.assertFalse(ImportOperation.objects.exists())

        dead, candidate = crashed_preparation("harris")
        reconcile_dead_operations("harris", actor="container start")
        dead.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual((dead.status, candidate.state), ("failed", "blocked"))
        reconciler = ImportOperation.objects.exclude(pk=dead.pk).get()
        self.assertEqual(
            (reconciler.intent, reconciler.actor, reconciler.status),
            ("reconcile", "container start", "completed"),
        )
