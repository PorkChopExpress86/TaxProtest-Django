"""County-scoped database ownership; this module does not execute ETL."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from django.db import connection

from counties.common.models import CountyWriter, ImportOperation

_LOCK_KEYS = {"harris": 742101, "brazos": 742102}


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
            cursor.execute("SELECT pg_try_advisory_lock(%s), pg_backend_pid()", [key])
            locked, pid = cursor.fetchone()
        if not locked:
            owner = CountyWriter.objects.filter(county=county).first()
            raise WriterConflict(county, owner.operation_id if owner else None)
    reserved = False
    try:
        writer, _ = CountyWriter.objects.get_or_create(county=county)
        if writer.operation_id is not None:
            raise WriterConflict(
                county, writer.operation_id, uncertain=not _lock_held(county, writer.backend_pid)
            )
        reserved = bool(
            CountyWriter.objects.filter(county=county, operation__isnull=True).update(
                operation=operation, backend_pid=pid
            )
        )
        if not reserved:
            writer.refresh_from_db()
            raise WriterConflict(county, writer.operation_id)
        yield
    finally:
        # A lost database session leaves durable uncertainty, even if execution reconnects.
        ended_safely = connection.vendor != "postgresql" or _lock_held(county, pid)
        if reserved and ended_safely:
            CountyWriter.objects.filter(county=county, operation=operation).update(
                operation=None, backend_pid=None
            )
        if locked and ended_safely:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
