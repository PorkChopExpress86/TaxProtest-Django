"""The Import disposition: what a finished Import operation did to published data.

Read from the operation's own classified status and the candidate its evidence names,
never stored (ADR-0023). Classification never writes the row and never rewords its
persisted evidence (ADR-0024).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from uuid import UUID

from django.urls import reverse

from counties.common.import_states import CandidateState, OperationStatus, operation_status_for
from counties.common.models import ImportCandidate, ImportOperation

# The intent every county runner records for an import that never prepares a candidate.
PREVIEW_INTENT = "preview"


class ImportDispositionKind(Enum):
    PUBLISHED = auto()
    ALREADY_APPLIED = auto()  # the named candidate was applied earlier; no new write
    HELD = auto()  # a held candidate awaits an operator; published data is unchanged
    PREVIEWED = auto()  # nothing was prepared or published
    FAILED = auto()  # published data is unchanged; adapters keep their own failure wording


@dataclass(frozen=True)
class ImportDisposition:
    kind: ImportDispositionKind
    operation_id: UUID
    candidate_id: UUID | None
    incomplete: bool = False
    notice: str | None = None  # the operator sentence, for kinds that have one


# The candidate state each held operation status was paired from.
_HELD_STATE = {
    operation_status_for(state): state
    for state in (CandidateState.PREPARED, CandidateState.AWAITING_REVIEW, CandidateState.BLOCKED)
}


def _named_candidate(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _candidate_and_operation(candidate_id: UUID | None, operation_id: UUID) -> str:
    if candidate_id is None:
        return f"Operation {operation_id}"
    return f"Candidate {candidate_id}; operation {operation_id}"


def _review_link() -> str:
    meta = ImportCandidate._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")


def disposition_for(operation_id: UUID | str, *, subject: str) -> ImportDisposition:
    """Classify the finished Import operation with this id; see ``import_disposition``."""
    return import_disposition(ImportOperation.objects.get(pk=operation_id), subject=subject)


def import_disposition(operation: ImportOperation, *, subject: str) -> ImportDisposition:
    """Classify a finished Import operation; ``subject`` names it in the operator notice.

    Raises ``ValueError`` for an operation that has not finished or ended in a way no
    import can, but never for a published one.
    """
    status = operation.status
    if status == OperationStatus.PUBLISHED:
        # Published data is live, so nothing about the evidence may make this raise.
        return ImportDisposition(
            ImportDispositionKind.PUBLISHED,
            operation.pk,
            _named_candidate(operation.evidence.get("candidate_id")),
        )
    if status == OperationStatus.ALREADY_APPLIED:
        # An idempotent apply names its candidate under its own persisted evidence key.
        candidate_id = _named_candidate(operation.evidence.get("already_applied"))
        return ImportDisposition(
            ImportDispositionKind.ALREADY_APPLIED,
            operation.pk,
            candidate_id,
            notice=(
                f"{subject} already applied earlier; no new write. "
                f"{_candidate_and_operation(candidate_id, operation.pk)}."
            ),
        )
    if operation.intent == PREVIEW_INTENT and status in (
        OperationStatus.COMPLETED,
        OperationStatus.COMPLETED_WITH_WARNINGS,
        OperationStatus.PARTIAL,
    ):
        # A preview prepares no candidate, so even a partial one holds nothing.
        return ImportDisposition(
            ImportDispositionKind.PREVIEWED,
            operation.pk,
            None,
            incomplete=status == OperationStatus.PARTIAL,
        )
    if status in _HELD_STATE or status == OperationStatus.PARTIAL:
        # A best-effort run that loaded only some sources always blocks its candidate.
        incomplete = status == OperationStatus.PARTIAL
        held = CandidateState.BLOCKED if incomplete else _HELD_STATE[OperationStatus(status)]
        candidate_id = _named_candidate(operation.evidence.get("candidate_id"))
        return ImportDisposition(
            ImportDispositionKind.HELD,
            operation.pk,
            candidate_id,
            incomplete=incomplete,
            notice=(
                f"{subject} {held}{' (incomplete)' if incomplete else ''}; "
                f"published data unchanged. {_candidate_and_operation(candidate_id, operation.pk)}. "
                f"Review in Django admin {_review_link()}."
            ),
        )
    if status == OperationStatus.FAILED:
        return ImportDisposition(
            ImportDispositionKind.FAILED,
            operation.pk,
            _named_candidate(operation.evidence.get("candidate_id")),
        )
    raise ValueError(f"Import operation {operation.pk} has no disposition for status {status}")
