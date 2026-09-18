"""County-scoped database ownership; this module does not execute ETL."""

import hashlib
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import UUID

from django.db import DatabaseError, connection, transaction

from counties.common.models import CountyWriter, ImportAuditEntry, ImportOperation

_LOCK_KEYS = {"harris": 742101, "brazos": 742102}
_CURRENT_WRITER: ContextVar[ImportOperation | None] = ContextVar("county_writer", default=None)


class FencedWriter(RuntimeError):
    """Execution no longer owns the reservation and cannot resume writing."""


class RecoveryRejected(RuntimeError):
    pass


def _source_digests(root: Path) -> dict[str, str]:
    digests = {}
    for directory, children, files in os.walk(root):
        children[:] = [name for name in children if name != ".imports"]
        for filename in sorted(files):
            source = Path(directory) / filename
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digests[str(source.relative_to(root))] = digest.hexdigest()
    return digests


def working_source_root(root: Path, *, reuse: bool = True) -> Path:
    """Isolate attempt-owned files, so a lost worker cannot overwrite another attempt."""
    operation = _CURRENT_WRITER.get()
    if operation is None:
        return root
    working = root / ".imports" / str(operation.pk)
    if not working.exists() and not reuse:
        working.mkdir(parents=True)
        operation.evidence.setdefault("working_sources", []).append(str(working))
        return working
    if not working.exists():
        base_digests = _source_digests(root)
        source_root = root
        for prior in (
            ImportOperation.objects.filter(
                county=operation.county,
                status__in=(
                    "completed",
                    "completed_with_warnings",
                    "partial",
                    "published",
                    "validated",
                    "prepared",
                    "awaiting_review",
                ),
            )
            .exclude(pk=operation.pk)
            .exclude(candidate__state__in=("rejected", "superseded"))
        ):
            retained = root / ".imports" / str(prior.pk)
            if str(retained) not in prior.evidence.get("working_sources", []):
                continue
            if prior.evidence.get("base_sources", {}).get(str(root)) != base_digests:
                break  # Explicitly changed base inputs take precedence over cached outputs.
            expected = prior.evidence.get("retained_source_digests", {}).get(str(retained))
            if expected is None or not retained.exists() or _source_digests(retained) != expected:
                raise RuntimeError(f"Retained source files changed or are unavailable: {prior.pk}")
            source_root = retained
            operation.evidence.setdefault("reused_from", {})[str(root)] = str(prior.pk)
            break
        operation.evidence.setdefault("base_sources", {})[str(root)] = base_digests
        working.mkdir(parents=True)
        for source in source_root.iterdir():
            if source.name == ".imports":
                continue
            target = working / source.name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        operation.evidence.setdefault("working_sources", []).append(str(working))
    return working


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
        try:
            operation.evidence["retained_source_digests"] = {
                path: _source_digests(Path(path))
                for path in operation.evidence.get("working_sources", [])
            }
        except OSError as exc:
            operation.warnings.append(f"Retained source verification unavailable: {exc}")
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
    except (RecoveryRejected, DatabaseError) as exc:
        explanation = (
            str(exc)
            if isinstance(exc, RecoveryRejected)
            else "Unable to verify release: writer reservation is busy or unavailable"
        )
        ImportAuditEntry.objects.create(
            operation=operation,
            kind="writer_recovery",
            actor=actor,
            reason=reason,
            evidence={**evidence, "explanation": explanation},
            result="rejected",
        )
        raise RecoveryRejected(explanation) from exc
    finally:
        if locked:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
