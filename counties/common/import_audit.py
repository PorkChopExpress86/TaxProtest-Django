"""Audit/ownership primitives; county callers select and interpret their sources."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.db.models import Q
from django.utils import timezone

from counties.common.candidate_staging import drop_candidate_schema
from counties.common.county_registry import registration_for
from counties.common.import_logging import import_warnings
from counties.common.import_source_digests import digest_once, file_digest, is_source_file
from counties.common.import_states import CandidateState, OperationStatus
from counties.common.import_writers import (
    DEAD_OPERATION_ERROR,
    county_writer,
    ensure_writer_free,
    fenced_write,
)
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


def record_sources(operation: ImportOperation, paths: list[Path], **identity) -> None:
    sources = operation.evidence.setdefault("sources", [])
    for path in paths:
        if not is_source_file(path):
            continue
        sources.append({"path": str(path), "sha256": file_digest(path), **identity})


@contextmanager
def audited_operation(
    county: str,
    intent: str,
    *,
    actor: str = "",
    origin: str = "command",
    requested_year: int | None = None,
    evidence: dict | None = None,
    publication_before: dict | None = None,
) -> Iterator[ImportOperation]:
    """Run one Import operation under the county writer reservation.

    The caller may classify ``operation.status`` before the block ends; otherwise it
    completes. A failure after a committed publication is recorded as a warning and
    not raised, because the published dataset is live. Any other failure fails the
    operation and is raised.
    """
    registration = registration_for(county)  # an unknown county records no operation
    operation = ImportOperation.objects.create(
        county=county,
        intent=intent,
        actor=actor,
        origin=origin,
        requested_year=requested_year,
        evidence=evidence or {},
        publication_before=publication_before,
    )
    warnings: list[str] = []
    try:
        with (
            import_warnings(
                registration.warning_logger, operation_id=str(operation.pk)
            ) as warnings,
            digest_once(),
            county_writer(operation),
        ):
            _reconcile_dead_operations(operation)
            yield operation
        if operation.status == OperationStatus.RUNNING:
            operation.status = (
                OperationStatus.COMPLETED_WITH_WARNINGS
                if operation.warnings
                else OperationStatus.COMPLETED
            )
        operation.status = OperationStatus(operation.status)
    except Exception as exc:
        if ImportOperation.objects.filter(
            pk=operation.pk, status=OperationStatus.PUBLISHED
        ).exists():
            operation.refresh_from_db(fields=["status", "publication_before", "publication_after"])
            operation.warnings.append(str(exc))
            operation.evidence["post_publication_failure"] = str(exc)
            return
        if operation.status == OperationStatus.COMPLETED_WITH_WARNINGS:
            operation.warnings.append(str(exc))
        else:
            # Publication values recorded by a rolled-back transaction were never observed.
            operation.refresh_from_db(fields=["publication_before", "publication_after"])
            operation.status = OperationStatus.FAILED
            operation.errors.append(str(exc))
        raise
    finally:
        operation.warnings = list(dict.fromkeys([*operation.warnings, *warnings]))
        operation.finished_at = timezone.now()
        operation.save()


def reconcile_dead_operations(county: str, *, actor: str) -> None:
    """Take ``county``'s writer when an operation may have died, so acquiring it reconciles.

    Raises ``WriterConflict``, and records no operation, while another operation still
    holds the writer.
    """
    if _dead_operations(county).exists() or _abandoned_candidates(county).exists():
        ensure_writer_free(county)
        with audited_operation(county, "operation_reconciliation", actor=actor):
            pass


def _dead_operations(county: str):
    # Unfinished operations that held the county writer: preparation creates a candidate
    # only inside that reservation and leaves it preparing or blocked only when it did
    # not record how it ended; any other operation records when it took the writer.
    return ImportOperation.objects.filter(county=county, finished_at__isnull=True).filter(
        Q(candidate__state__in=(CandidateState.PREPARING, CandidateState.BLOCKED))
        | Q(candidate__isnull=True, writer_held_at__isnull=False)
    )


def _abandoned_candidates(county: str):
    # A preparing candidate whose operation recorded an ending will never be prepared.
    return ImportCandidate.objects.filter(
        county=county, state=CandidateState.PREPARING, operation__finished_at__isnull=False
    )


def _block(candidate: ImportCandidate) -> None:
    if candidate.state == CandidateState.PREPARING:
        candidate.state = CandidateState.BLOCKED
        candidate.evidence["error"] = DEAD_OPERATION_ERROR
        candidate.save(update_fields=["state", "evidence"])


def _reconcile_dead_operations(operation: ImportOperation) -> None:
    """Fail operations that held the county writer before ``operation`` and never finished.

    ``operation`` now holds the writer, so an earlier holder is fenced for good: its
    worker died before its own cleanup recorded how it ended.
    """
    unfinished = _dead_operations(operation.county).exclude(pk=operation.pk)
    for pk in unfinished.values_list("pk", flat=True):
        candidate = None
        with fenced_write():
            dead = unfinished.select_for_update(of=("self",)).filter(pk=pk).first()
            if dead is None:
                continue  # its worker recorded how it ended after all
            dead.status = OperationStatus.FAILED
            dead.errors.append(DEAD_OPERATION_ERROR)
            dead.finished_at = timezone.now()
            dead.save(update_fields=["status", "errors", "finished_at"])
            evidence = {"reconciled_by": str(operation.pk)}
            candidate = ImportCandidate.objects.filter(operation=dead).first()
            if candidate is not None:
                evidence.update(
                    candidate_id=str(candidate.pk), candidate_state_before=candidate.state
                )
                _block(candidate)
            ImportAuditEntry.objects.create(
                operation=dead,
                kind="operation_reconciliation",
                actor=operation.actor,
                reason=DEAD_OPERATION_ERROR,
                evidence=evidence,
                result=OperationStatus.FAILED,
            )
        if candidate is not None:
            drop_candidate_schema(candidate)
    abandoned = _abandoned_candidates(operation.county).exclude(operation=operation)
    for pk in abandoned.values_list("pk", flat=True):
        with fenced_write():
            candidate = abandoned.select_for_update(of=("self",)).filter(pk=pk).first()
            if candidate is None:
                continue
            ImportAuditEntry.objects.create(
                operation=candidate.operation,
                kind="operation_reconciliation",
                actor=operation.actor,
                reason=DEAD_OPERATION_ERROR,
                evidence={
                    "reconciled_by": str(operation.pk),
                    "candidate_id": str(candidate.pk),
                    "candidate_state_before": candidate.state,
                },
                result=CandidateState.BLOCKED,
            )
            _block(candidate)
        drop_candidate_schema(candidate)
