"""Blocking a candidate drops its staged schema in the transaction that blocks it (#190, #192).

A failure inside that transaction leaves the candidate either not blocked with its schema, or
blocked without it, never blocked with a schema. Failures are injected at the staged schema
drop's SQL statement, and by a separate database session that plays the worker; both stay put
when the state writes move (#193).
"""

from contextlib import contextmanager

import psycopg2
from django.db import DatabaseError, OperationalError, connection
from django.test import TransactionTestCase

from counties.common.candidate_lifecycle import prepare
from counties.common.county_registry import registration_for
from counties.common.import_audit import audited_operation
from counties.common.import_retention import cleanup_sources
from counties.common.import_writers import _operation_lock_key, recover_writer
from counties.common.models import (
    CountyWriter,
    ImportAuditEntry,
    ImportCandidate,
    ImportOperation,
)
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)
from counties.common.tests.test_candidate_transition_pins import fail_after_schema_drop, stored
from counties.common.tests.test_dead_operation_reconciliation import DEAD, next_operation

# A test-side bound on every lock wait of the reconciling session: a wait fails, never hangs.
WAIT_LIMIT = "3s"


class WorkerStopped(BaseException):
    """A crash that no ``except Exception`` handler sees, such as the worker being killed."""


def crash_after_schema_drop(execute, sql, params, many, context):
    """Let a staged schema drop run, then stop the worker before its transaction commits."""
    result = execute(sql, params, many, context)
    if sql.startswith("DROP SCHEMA"):
        raise WorkerStopped("Worker stopped after dropping the staged schema")
    return result


def fail_first_schema_drop():
    """Fail the transaction of the first staged schema drop only; later drops succeed."""
    failed = []

    def wrapper(execute, sql, params, many, context):
        if sql.startswith("DROP SCHEMA") and not failed:
            failed.append(sql)
            return fail_after_schema_drop(execute, sql, params, many, context)
        return execute(sql, params, many, context)

    return wrapper


def refuse_schema_drops(execute, sql, params, many, context):
    """Every staged schema drop fails, as it would while the database refuses it."""
    if sql.startswith("DROP SCHEMA"):
        raise DatabaseError("Staged schema drop refused")
    return execute(sql, params, many, context)


@contextmanager
def lock_waits_fail_fast():
    with connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{WAIT_LIMIT}'")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET lock_timeout")


class WorkerSession:
    """A database session of its own, playing the worker of one Import operation."""

    def __init__(self):
        self.connection = psycopg2.connect(**connection.get_connection_params())
        self.pid = self.query("SELECT pg_backend_pid()")[0]
        self.connection.commit()

    def query(self, sql, params=()):
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    def keep_operation_alive(self, operation):
        """Take the session lock an operation's worker holds until its ending is recorded."""
        self.query("SELECT pg_advisory_lock(%s, %s)", _operation_lock_key(operation.pk))
        self.connection.commit()

    def hold_county_writer(self, operation):
        """Own the county writer, as a worker that died mid-operation leaves it."""
        key = registration_for(operation.county).writer_lock_key
        self.query("SELECT pg_advisory_lock(%s)", [key])
        self.connection.commit()
        CountyWriter.objects.update_or_create(
            county=operation.county, defaults={"operation": operation, "backend_pid": self.pid}
        )

    def write_staged_rows(self, candidate, table, column):
        """Leave a transaction open that wrote the candidate's staged table."""
        schema = candidate.storage_schema
        with self.connection.cursor() as cursor:
            cursor.execute(f'UPDATE "{schema}"."{table}" SET "{column}" = "{column}"')

    def terminate(self):
        with connection.cursor() as cursor:
            # Waits until the session has ended, so no lock of it remains.
            cursor.execute("SELECT pg_terminate_backend(%s, 10000)", [self.pid])
            if not cursor.fetchone()[0]:
                raise AssertionError(f"Worker session {self.pid} did not end")
        self.close()

    def close(self):
        try:
            self.connection.close()
        except psycopg2.Error:
            pass  # a terminated session is already closed


def granted_lock_holders(candidate, table) -> set[int]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pid FROM pg_locks WHERE relation = %s::regclass AND granted",
            [f'"{candidate.storage_schema}"."{table}"'],
        )
        return {pid for (pid,) in cursor.fetchall()}


class BlockedCandidateAtomicity:
    """Behaviour every county port gives a candidate that is blocked when a failure strikes."""

    def prepare_blocked(self):
        with audited_operation(self.county, "fixture", actor="operator") as operation:
            prepare(operation, self.fixture_load(operation, ready=False))

    def blocked_preparation(self):
        return ImportCandidate.objects.exclude(pk=self.candidate.pk).get()

    def crashed(self, *, ending_recorded: bool) -> ImportCandidate:
        """Leave the contract's candidate, staged tables and all, as a dead preparation does.

        With ``ending_recorded`` its worker saved how the operation ended and then died
        before saving the candidate; without, it died before recording anything.
        """
        ImportCandidate.objects.filter(pk=self.candidate.pk).update(state="preparing")
        if not ending_recorded:
            ImportOperation.objects.filter(pk=self.candidate.operation_id).update(finished_at=None)
        return ImportCandidate.objects.get(pk=self.candidate.pk)

    def reconciliations(self, candidate):
        return ImportAuditEntry.objects.filter(
            kind="operation_reconciliation", operation=candidate.operation_id
        )

    def assertNeverBlockedWithSchema(self, candidate):
        now = stored(candidate)
        self.assertFalse(now["state"] == "blocked" and now["schema"], now)

    def assertLeftPreparingWithSchema(self, candidate):
        self.assertNeverBlockedWithSchema(candidate)
        now = stored(candidate)
        self.assertEqual((now["state"], now["schema"]), ("preparing", True))
        self.assertFalse(self.reconciliations(candidate).exists())

    def assertReconciled(self, candidate, *, operation_failed: bool):
        now = stored(candidate)
        self.assertEqual((now["state"], now["schema"]), ("blocked", False))
        self.assertEqual(now["evidence"]["error"], DEAD)
        self.assertEqual(self.reconciliations(candidate).count(), 1)
        if operation_failed:
            dead = ImportOperation.objects.get(pk=candidate.operation_id)
            self.assertEqual((dead.status, dead.errors), ("failed", [DEAD]))
            self.assertIsNotNone(dead.finished_at)

    # Preparation ending blocked.

    def test_a_crash_while_preparation_ends_blocked_leaves_it_unblocked_with_its_schema(self):
        with connection.execute_wrapper(crash_after_schema_drop), self.assertRaises(WorkerStopped):
            self.prepare_blocked()

        candidate = self.blocked_preparation()
        self.assertLeftPreparingWithSchema(candidate)

        # Its operation recorded an ending, so the next writer blocks it and drops its schema.
        next_operation(self.county)
        self.assertReconciled(candidate, operation_failed=False)

    def test_a_failed_drop_while_preparation_ends_blocked_falls_back_to_blocked_without_schema(
        self,
    ):
        with (
            connection.execute_wrapper(fail_first_schema_drop()),
            self.assertRaisesMessage(OSError, "Failed after dropping"),
        ):
            self.prepare_blocked()

        candidate = self.blocked_preparation()
        self.assertNeverBlockedWithSchema(candidate)
        now = stored(candidate)
        self.assertEqual((now["state"], now["schema"]), ("blocked", False))
        self.assertEqual(
            now["evidence"],
            {"error": "Candidate was not saved: Failed after dropping the staged schema"},
        )

    def test_a_drop_that_keeps_failing_leaves_the_fallback_blocked_with_its_schema(self):
        # Not atomic: the best-effort fallback after a failed save stays as it is (#192), and
        # source retention's sweep reclaims the schema it leaves.
        with (
            connection.execute_wrapper(refuse_schema_drops),
            self.assertRaisesMessage(DatabaseError, "Staged schema drop refused"),
        ):
            self.prepare_blocked()

        candidate = self.blocked_preparation()
        now = stored(candidate)
        self.assertEqual((now["state"], now["schema"]), ("blocked", True))
        self.assertEqual(
            now["evidence"], {"error": "Candidate was not saved: Staged schema drop refused"}
        )

        cleanup = cleanup_sources(self.county, actor="operator", reason="Reclaim space")
        self.assertEqual(cleanup.evidence["dropped_schemas"], [candidate.storage_schema])
        self.assertFalse(stored(candidate)["schema"])

    # Reconciliation, at both of its call sites.

    def test_a_failure_while_reconciling_a_dead_operation_leaves_it_unblocked_with_its_schema(
        self,
    ):
        candidate = self.crashed(ending_recorded=False)

        with connection.execute_wrapper(fail_after_schema_drop), self.assertRaises(OSError):
            next_operation(self.county)

        self.assertLeftPreparingWithSchema(candidate)
        dead = ImportOperation.objects.get(pk=candidate.operation_id)
        self.assertEqual((dead.finished_at, dead.errors), (None, []))

        next_operation(self.county)
        self.assertReconciled(candidate, operation_failed=True)

    def test_a_failure_while_reconciling_an_abandoned_candidate_leaves_it_unblocked_with_schema(
        self,
    ):
        candidate = self.crashed(ending_recorded=True)

        with connection.execute_wrapper(fail_after_schema_drop), self.assertRaises(OSError):
            next_operation(self.county)

        self.assertLeftPreparingWithSchema(candidate)

        next_operation(self.county)
        self.assertReconciled(candidate, operation_failed=False)

    def test_an_abandoned_candidates_drop_waits_on_a_session_holding_its_staged_tables(self):
        # This call site checks no liveness: it waits for any session that still holds a
        # staged table, and a wait that fails leaves the candidate unblocked with its schema.
        candidate = self.crashed(ending_recorded=True)
        table, column = self.candidate_column
        holder = WorkerSession()
        try:
            holder.write_staged_rows(candidate, table, column)
            self.assertIn(holder.pid, granted_lock_holders(candidate, table))
            with lock_waits_fail_fast(), self.assertRaises(OperationalError):
                next_operation(self.county)
            self.assertLeftPreparingWithSchema(candidate)
        finally:
            holder.close()

        with lock_waits_fail_fast():
            next_operation(self.county)
        self.assertReconciled(candidate, operation_failed=False)

    # Reconciliation does not wait on the dead worker.

    def test_reconciliation_leaves_an_operation_alone_while_its_worker_session_lives(self):
        candidate = self.crashed(ending_recorded=False)
        table, column = self.candidate_column
        worker = WorkerSession()
        try:
            worker.keep_operation_alive(candidate.operation)
            worker.write_staged_rows(candidate, table, column)
            self.assertIn(worker.pid, granted_lock_holders(candidate, table))

            with lock_waits_fail_fast():
                operation = next_operation(self.county)

            self.assertEqual(operation.status, "completed")
            self.assertLeftPreparingWithSchema(candidate)
            alive = ImportOperation.objects.get(pk=candidate.operation_id)
            self.assertEqual((alive.finished_at, alive.errors), (None, []))
        finally:
            worker.close()

    def test_reconciliations_drop_does_not_wait_on_a_dead_workers_staged_locks(self):
        candidate = self.crashed(ending_recorded=False)
        table, column = self.candidate_column
        worker = WorkerSession()
        try:
            worker.keep_operation_alive(candidate.operation)
            worker.write_staged_rows(candidate, table, column)
            self.assertIn(worker.pid, granted_lock_holders(candidate, table))
            worker.terminate()
        finally:
            worker.close()

        with lock_waits_fail_fast():
            next_operation(self.county)
        self.assertReconciled(candidate, operation_failed=True)

    def test_a_dead_worker_that_held_the_writer_is_recovered_then_reconciled_without_waiting(
        self,
    ):
        candidate = self.crashed(ending_recorded=False)
        table, column = self.candidate_column
        worker = WorkerSession()
        try:
            worker.keep_operation_alive(candidate.operation)
            worker.hold_county_writer(candidate.operation)
            worker.write_staged_rows(candidate, table, column)
            self.assertIn(worker.pid, granted_lock_holders(candidate, table))
            worker.terminate()
        finally:
            worker.close()

        recover_writer(candidate.operation, actor="operator", reason="Worker host lost")
        with lock_waits_fail_fast():
            next_operation(self.county)
        self.assertReconciled(candidate, operation_failed=True)

    def test_an_abandoned_candidates_drop_does_not_wait_on_a_worker_that_recorded_its_ending(
        self,
    ):
        # A worker that recorded its ending is outside every transaction, though its session
        # may still hold the liveness lock; this call site does not check that lock.
        candidate = self.crashed(ending_recorded=True)
        worker = WorkerSession()
        try:
            worker.keep_operation_alive(candidate.operation)
            with lock_waits_fail_fast():
                next_operation(self.county)
            self.assertReconciled(candidate, operation_failed=False)
        finally:
            worker.close()


class HarrisBlockedCandidateAtomicity(
    BlockedCandidateAtomicity, HarrisCandidateContract, TransactionTestCase
):
    pass


class BrazosBlockedCandidateAtomicity(
    BlockedCandidateAtomicity, BrazosCandidateContract, TransactionTestCase
):
    pass
