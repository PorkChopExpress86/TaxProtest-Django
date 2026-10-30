"""County-scoped database ownership; this module does not execute ETL."""

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import NamedTuple
from uuid import UUID

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from counties.common.county_registry import registration_for
from counties.common.import_source_digests import carry_digests, tree_digests
from counties.common.import_states import CandidateState, LegacyOperationStatus, OperationStatus
from counties.common.models import CountyWriter, ImportAuditEntry, ImportCandidate, ImportOperation

_CURRENT_WRITER: ContextVar[ImportOperation | None] = ContextVar("county_writer", default=None)


DEAD_OPERATION_ERROR = (
    "Import operation never finished: its worker stopped, and another Import operation "
    "has since held the county writer"
)


RECOVERED_OPERATION_ERROR = (
    "Import operation never finished: its worker stopped, and its county writer was recovered"
)

# Two-key advisory locks live apart from the one-key county writer locks; the first
# key names this lock family, the second is drawn from the operation's id.
_OPERATION_LOCK_FAMILY = 0x54504F50


def _operation_lock_key(operation_id: UUID | str) -> list[int]:
    return [_OPERATION_LOCK_FAMILY, UUID(str(operation_id)).int % 2**31]


@contextmanager
def operation_alive(operation: ImportOperation) -> Iterator[None]:
    """Hold a session lock from an operation's start until its ending is recorded.

    Another session that can take the lock knows the operation's worker is gone.
    """
    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", _operation_lock_key(operation.pk))
    try:
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", _operation_lock_key(operation.pk)
                )
        except DatabaseError:
            pass  # a lost session already released it


def operation_is_alive(operation_id: UUID | str) -> bool:
    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", _operation_lock_key(operation_id))
        if not cursor.fetchone()[0]:
            return True
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", _operation_lock_key(operation_id))
    return False


class FencedWriter(RuntimeError):
    """Execution no longer owns the reservation and cannot resume writing."""


class RecoveryRejected(RuntimeError):
    pass


def _active_writer() -> ImportOperation:
    operation = _CURRENT_WRITER.get()
    if operation is None:
        raise FencedWriter("No county writer reservation is active; mutation rejected")
    return operation


def _working_root(root: Path, operation_id: UUID | str) -> Path:
    return root / ".imports" / str(operation_id)


class ReusedSource(NamedTuple):
    base_root: Path
    prior: ImportOperation


def reused_source(operation: ImportOperation, working: Path) -> ReusedSource | None:
    """Name the base root and prior operation whose retained files ``working`` copied.

    ``working`` must be a working root of ``operation``, as ``working_source_root`` returns.
    Returns ``None`` when it copied the base root itself.
    """
    base_root = working.parent.parent
    if working != _working_root(base_root, operation.pk):
        raise ValueError(f"Not a working source root of {operation.pk}: {working}")
    prior_id = operation.evidence.get("reused_from", {}).get(str(base_root))
    if prior_id is None:
        return None
    return ReusedSource(base_root, ImportOperation.objects.get(pk=prior_id))


def working_source_root(root: Path, *, reuse: bool = True) -> Path:
    """Isolate attempt-owned files, so a lost worker cannot overwrite another attempt."""
    operation = _active_writer()
    working = _working_root(root, operation.pk)
    if not working.exists() and not reuse:
        working.mkdir(parents=True)
        operation.evidence.setdefault("working_sources", []).append(str(working))
        return working
    if not working.exists():
        base_digests = tree_digests(root)
        source_root = root
        for prior in (
            ImportOperation.objects.filter(
                county=operation.county,
                status__in=(
                    OperationStatus.COMPLETED,
                    OperationStatus.COMPLETED_WITH_WARNINGS,
                    OperationStatus.PARTIAL,
                    OperationStatus.PUBLISHED,
                    LegacyOperationStatus.VALIDATED,
                    OperationStatus.PREPARED,
                    OperationStatus.AWAITING_REVIEW,
                ),
            )
            .exclude(pk=operation.pk)
            .exclude(candidate__state__in=(CandidateState.REJECTED, CandidateState.SUPERSEDED))
        ):
            retained = _working_root(root, prior.pk)
            if str(retained) not in prior.evidence.get("working_sources", []):
                continue
            if prior.evidence.get("base_sources", {}).get(str(root)) != base_digests:
                break  # Explicitly changed base inputs take precedence over cached outputs.
            expected = prior.evidence.get("retained_source_digests", {}).get(str(retained))
            if expected is None or not retained.exists():
                operation.warnings.append(
                    f"Retained sources of {prior.pk} are unavailable; copied the base root"
                )
                break
            if tree_digests(retained) != expected:
                raise RuntimeError(f"Retained source files changed or are unavailable: {prior.pk}")
            source_root = retained
            operation.evidence.setdefault("reused_from", {})[str(root)] = str(prior.pk)
            break
        operation.evidence.setdefault("base_sources", {})[str(root)] = base_digests
        working.mkdir(parents=True)
        try:
            for source in source_root.iterdir():
                if source.name == ".imports":
                    continue
                target = working / source.name
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
        except BaseException:
            shutil.rmtree(working, ignore_errors=True)
            raise
        carry_digests(source_root, working)
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
            [pid, registration_for(county).writer_lock_key],
        )
        return bool(cursor.fetchone()[0])


def fail_operation(operation: ImportOperation, error: str) -> None:
    """Record that an unfinished operation's worker stopped before it could end it."""
    operation.status = OperationStatus.FAILED
    operation.errors.append(error)
    operation.finished_at = timezone.now()
    operation.save(update_fields=["status", "errors", "finished_at"])


def ensure_writer_free(county: str) -> None:
    """Raise ``WriterConflict`` while an operation holds or uncertainly holds ``county``."""
    writer = CountyWriter.objects.filter(county=county, operation__isnull=False).first()
    if writer is not None:
        raise WriterConflict(
            county, writer.operation_id, uncertain=not _lock_held(county, writer.backend_pid)
        )


@contextmanager
def county_writer(operation: ImportOperation) -> Iterator[None]:
    county = operation.county
    key = registration_for(county).writer_lock_key
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
        ensure_writer_free(county)
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
        operation.writer_held_at = timezone.now()
        ImportOperation.objects.filter(pk=operation.pk).update(
            writer_held_at=operation.writer_held_at
        )
        # Publish the owner before taking the session lock, so even acquisition
        # contention has a durable identity. Never clear ownership before unlock.
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
                locked = cursor.fetchone()[0]
            if not locked:
                # Holding no lock, this attempt fenced nobody, so its reservation goes too.
                CountyWriter.objects.filter(county=county, operation=operation).update(
                    operation=None, backend_pid=None
                )
                reserved = False
                operation.writer_held_at = None
                ImportOperation.objects.filter(pk=operation.pk).update(writer_held_at=None)
                raise WriterConflict(county, operation.pk, uncertain=True)
        token = _CURRENT_WRITER.set(operation)
        yield
    finally:
        try:
            operation.evidence["retained_source_digests"] = {
                path: tree_digests(Path(path))
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
    operation = _active_writer()
    with transaction.atomic():
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
    key = registration_for(operation.county).writer_lock_key
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
            fenced = ImportOperation.objects.select_for_update().get(pk=operation.pk)
            # A preparation's candidate and schema are retired by the next writer's
            # reconciliation, which needs its operation still unfinished.
            if (
                fenced.finished_at is None
                and not ImportCandidate.objects.filter(operation=fenced).exists()
            ):
                fail_operation(fenced, RECOVERED_OPERATION_ERROR)
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
