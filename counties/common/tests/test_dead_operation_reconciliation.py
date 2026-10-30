"""An Import operation whose worker died is failed by the next writer of its county (#133)."""

from threading import Event, Thread
from uuid import uuid4

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from counties.common.county_registry import registration_for
from counties.common.import_audit import audited_operation, reconcile_dead_operations
from counties.common.import_writers import (
    WriterConflict,
    county_writer,
    operation_alive,
    recover_writer,
)
from counties.common.models import (
    CountyWriter,
    ImportAuditEntry,
    ImportCandidate,
    ImportOperation,
)
from counties.common.tests.test_candidate_schema_retirement import (
    leave_schema_behind,
    schema_exists,
)

DEAD = (
    "Import operation never finished: its worker stopped, and another Import operation "
    "has since held the county writer"
)
RECOVERED = (
    "Import operation never finished: its worker stopped, and its county writer was recovered"
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

    def test_an_operation_whose_candidate_awaits_review_is_left_alone(self):
        unfinished, candidate = crashed_preparation("harris", state="awaiting_review")
        next_operation("harris")
        unfinished.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual((unfinished.status, candidate.state), ("running", "awaiting_review"))
        self.assertTrue(schema_exists(candidate))

    def test_a_preparing_candidate_behind_an_operation_that_recorded_its_status_is_blocked(self):
        # The worker saved the operation's ending, then died before saving its candidate.
        dead, candidate = crashed_preparation("harris")
        ImportOperation.objects.filter(pk=dead.pk).update(status="awaiting_review")
        next_operation("harris")
        dead.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual((dead.status, dead.errors), ("failed", [DEAD]))
        self.assertEqual((candidate.state, candidate.evidence), ("blocked", {"error": DEAD}))
        self.assertFalse(schema_exists(candidate))

    def test_a_preparing_candidate_behind_a_finished_operation_is_blocked(self):
        dead, candidate = crashed_preparation("harris")
        ImportOperation.objects.filter(pk=dead.pk).update(
            status="failed", errors=["Candidate save failed"], finished_at=timezone.now()
        )
        next_operation("harris")
        dead.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual((dead.status, dead.errors), ("failed", ["Candidate save failed"]))
        self.assertEqual((candidate.state, candidate.evidence), ("blocked", {"error": DEAD}))
        self.assertFalse(schema_exists(candidate))
        self.assertEqual(
            ImportAuditEntry.objects.get(operation=dead).evidence["candidate_state_before"],
            "preparing",
        )

    def test_an_operation_without_a_candidate_that_held_the_writer_is_failed(self):
        dead = ImportOperation.objects.create(county="harris", intent="apply")
        with county_writer(dead):
            pass  # its worker died before it recorded how it ended
        operation = next_operation("harris")
        dead.refresh_from_db()
        self.assertEqual((dead.status, dead.errors), ("failed", [DEAD]))
        self.assertIsNotNone(dead.finished_at)
        entry = ImportAuditEntry.objects.get(operation=dead)
        self.assertEqual(
            (entry.kind, entry.result, entry.evidence),
            ("operation_reconciliation", "failed", {"reconciled_by": str(operation.pk)}),
        )
        self.assertEqual(operation.status, "completed")

    def test_recovering_a_writer_fails_the_operation_it_fences(self):
        fenced = ImportOperation.objects.create(county="harris", intent="apply")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
        connection.close()  # the writer's database session ends
        CountyWriter.objects.create(county="harris", operation=fenced, backend_pid=pid)

        recover_writer(fenced, actor="operator", reason="Worker host lost")

        fenced.refresh_from_db()
        self.assertEqual((fenced.status, fenced.errors), ("failed", [RECOVERED]))
        self.assertIsNotNone(fenced.finished_at)

    def test_recovering_a_preparation_leaves_its_candidate_to_the_next_writer(self):
        fenced, candidate = crashed_preparation("harris")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
        connection.close()
        CountyWriter.objects.create(county="harris", operation=fenced, backend_pid=pid)

        recover_writer(fenced, actor="operator", reason="Worker host lost")
        next_operation("harris")

        fenced.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual((fenced.status, fenced.errors), ("failed", [DEAD]))
        self.assertEqual(candidate.state, "blocked")
        self.assertFalse(schema_exists(candidate))

    def test_an_operation_still_recording_its_ending_is_not_failed(self):
        # It has released the writer and is about to save how it ended.
        finishing = ImportOperation.objects.create(county="harris", intent="apply")
        with county_writer(finishing):
            pass
        held, release = Event(), Event()

        def finish_slowly():
            try:
                with operation_alive(finishing):
                    held.set()
                    release.wait(15)
            finally:
                connections.close_all()

        worker = Thread(target=finish_slowly)
        worker.start()
        try:
            self.assertTrue(held.wait(15))
            next_operation("harris")
        finally:
            release.set()
            worker.join(20)
        finishing.refresh_from_db()
        self.assertEqual((finishing.status, finishing.errors), ("running", []))
        self.assertFalse(ImportAuditEntry.objects.filter(operation=finishing).exists())

    def test_a_writer_that_cannot_take_the_lock_leaves_no_reservation(self):
        # Recovery cleared the reservation but still holds the lock for a moment.
        held, release = Event(), Event()
        key = registration_for("harris").writer_lock_key

        def hold_lock():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_lock(%s)", [key])
                    held.set()
                    release.wait(15)
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
            finally:
                connections.close_all()

        worker = Thread(target=hold_lock)
        worker.start()
        try:
            self.assertTrue(held.wait(15))
            loser = ImportOperation.objects.create(county="harris", intent="apply")
            with self.assertRaises(WriterConflict), county_writer(loser):
                pass
        finally:
            release.set()
            worker.join(20)
        self.assertIsNone(CountyWriter.objects.get(county="harris").operation_id)
        next_operation("harris")

    def test_reconciling_while_the_writer_is_held_records_no_operation(self):
        with audited_operation("harris", "holder", actor="operator") as holder:
            crashed_preparation("harris")
            for _ in range(2):
                with self.assertRaises(WriterConflict) as conflict:
                    reconcile_dead_operations("harris", actor="check_and_import_data")
                self.assertEqual(conflict.exception.operation_id, holder.pk)
                self.assertFalse(conflict.exception.uncertain)
        self.assertFalse(ImportOperation.objects.filter(intent="operation_reconciliation").exists())
