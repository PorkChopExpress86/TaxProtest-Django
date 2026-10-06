"""Candidate state and Import operation status, defined once (ADR-0023).

Two persisted vocabularies with byte-identical stored values. They are paired
explicitly, never by matching spelling. No ``choices`` or check constraint guards the
columns; these values are what the rows already hold.
"""

from enum import StrEnum


class OperationStatus(StrEnum):
    """The one status vocabulary of an Import operation."""

    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PARTIAL = "partial"
    PREPARED = "prepared"
    BLOCKED = "blocked"
    AWAITING_REVIEW = "awaiting_review"
    PUBLISHED = "published"
    ALREADY_APPLIED = "already_applied"
    FAILED = "failed"


class LegacyOperationStatus(StrEnum):
    """Stored operation statuses no current code writes, still read by source reuse."""

    VALIDATED = "validated"


class CandidateState(StrEnum):
    """The persisted position of a candidate in the Candidate lifecycle."""

    PREPARING = "preparing"
    PREPARED = "prepared"
    AWAITING_REVIEW = "awaiting_review"
    BLOCKED = "blocked"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


# Only these candidate states can end an Import operation.
_OPERATION_STATUS_FOR_CANDIDATE = {
    CandidateState.PREPARED: OperationStatus.PREPARED,
    CandidateState.AWAITING_REVIEW: OperationStatus.AWAITING_REVIEW,
    CandidateState.BLOCKED: OperationStatus.BLOCKED,
    CandidateState.PUBLISHED: OperationStatus.PUBLISHED,
}


def operation_status_for(state: CandidateState) -> OperationStatus:
    """The status of an Import operation that ended with its candidate in ``state``.

    Raises ``KeyError`` for a state no Import operation can end with.
    """
    return _OPERATION_STATUS_FOR_CANDIDATE[state]
