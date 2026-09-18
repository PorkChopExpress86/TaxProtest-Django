"""County-scoped database ownership; this module does not execute ETL."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from django.db import connection, transaction

from counties.common.models import CountyWriter, ImportAuditEntry, ImportOperation

_LOCK_KEYS = {"harris": 742101, "brazos": 742102}
_CURRENT_WRITER: ContextVar[ImportOperation | None] = ContextVar("county_writer", default=None)


class FencedWriter(RuntimeError):
    """Execution no longer owns the reservation and cannot resume writing."""


class RecoveryRejected(RuntimeError):
    pass


class WriterConflict(RuntimeError):
    def __init__(self, county: str, operation_id: UUID | None, *, uncertain: bool = False):
        self.operation_id = operation_id
        self.uncertain = uncertain
        state = "Recovery required" if uncertain else "Active writer"
        super().__init__(f"{county}: {state}: {operation_id}; competing import rejected")


def writer_status(county: str) -> str:
    writer = CountyWriter.objects.filter(county=county).first()
    if writer is None or writer.operation_id is None:
        return "Available"
    state = "Active writer" if _lock_held(county, writer.backend_pid) else "Recovery required"
    return f"{state}: {writer.operation_id}"


def _lock_held(county: str, pid: int | None) -> bool:
    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = %s AND classid = 0 AND objid = %s AND objsubid = 1 AND granted)",
            [pid, _LOCK_KEYS[county]],
        )
        return bool(cursor.fetchone()[0])


@contextmanager
def county_writer(operation: ImportOperation) -> Iterator[None]:
    county = operation.county
    key = _LOCK_KEYS[county]
    locked = False
    pid = None
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
    reserved = False
    token = None
    try:
        writer, _ = CountyWriter.objects.get_or_create(county=county)
        if writer.operation_id is not None:
            raise WriterConflict(
                county, writer.operation_id, uncertain=not _lock_held(county, writer.backend_pid)
            )
        while not reserved:
            reserved = bool(
                CountyWriter.objects.filter(county=county, operation__isnull=True).update(
                    operation=operation, backend_pid=pid
                )
            )
            if not reserved:
                writer.refresh_from_db()
                if writer.operation_id is not None:
                    raise WriterConflict(county, writer.operation_id)
                # The winner already ended; a currently free county may be acquired.
        # Publish the owner before taking the session lock, so even acquisition
        # contention has a durable identity. Never clear ownership before unlock.
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
                locked = cursor.fetchone()[0]
            if not locked:
                raise WriterConflict(county, operation.pk, uncertain=True)
        token = _CURRENT_WRITER.set(operation)
        yield
    finally:
        if token is not None:
            _CURRENT_WRITER.reset(token)
        # A lost database session leaves durable uncertainty, even if execution reconnects.
        ended_safely = connection.vendor != "postgresql" or _lock_held(county, pid)
        if locked and ended_safely:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
        if reserved and ended_safely:
            CountyWriter.objects.filter(county=county, operation=operation).update(
                operation=None, backend_pid=None
            )


@contextmanager
def fenced_write() -> Iterator[None]:
    """Hold the exact reservation while mutating, including nested persistence."""
    with transaction.atomic():
        operation = _CURRENT_WRITER.get()
        if operation is not None:
            writer = CountyWriter.objects.select_for_update().get(county=operation.county)
            if writer.operation_id != operation.pk:
                raise FencedWriter(f"Writer {operation.pk} has been fenced; mutation rejected")
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                if pid != writer.backend_pid or not _lock_held(operation.county, pid):
                    raise FencedWriter(f"Writer {operation.pk} lost its database session")
        yield


def recover_writer(operation: ImportOperation, *, actor: str, reason: str) -> None:
    """Prove the session ended, then invalidate its token under database fencing."""
    if not reason.strip():
        raise RecoveryRejected("A recovery reason is required")
    if connection.vendor != "postgresql":
        raise RecoveryRejected("Unable to verify writer ownership without PostgreSQL")
    key = _LOCK_KEYS[operation.county]
    evidence: dict = {}
    locked = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
            locked = cursor.fetchone()[0]
        if not locked:
            raise RecoveryRejected("Unable to verify release: writer session still owns the lock")
        with transaction.atomic():
            writer = (
                CountyWriter.objects.select_for_update(nowait=True)
                .filter(county=operation.county)
                .first()
            )
            if writer is None or writer.operation_id != operation.pk:
                raise RecoveryRejected("Stale recovery: this operation no longer owns the county")
            if writer.backend_pid is None:
                raise RecoveryRejected(
                    "Unable to verify release: database session was not recorded"
                )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = %s)",
                    [writer.backend_pid],
                )
                if cursor.fetchone()[0]:
                    raise RecoveryRejected(
                        "Unable to verify release: original database session is still present"
                    )
            evidence = {
                "database session ended": True,
                "backend_pid": writer.backend_pid,
                "fenced_operation": str(operation.pk),
            }
            writer.operation = None
            writer.backend_pid = None
            writer.save()
            ImportAuditEntry.objects.create(
                operation=operation,
                kind="writer_recovery",
                actor=actor,
                reason=reason,
                evidence=evidence,
                result="released",
            )
    except RecoveryRejected as exc:
        ImportAuditEntry.objects.create(
            operation=operation,
            kind="writer_recovery",
            actor=actor,
            reason=reason,
            evidence={**evidence, "explanation": str(exc)},
            result="rejected",
        )
        raise
    finally:
        if locked:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
