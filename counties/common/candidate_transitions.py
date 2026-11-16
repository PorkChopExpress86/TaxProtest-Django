"""Candidate transitions: the only code that writes Candidate state.

Each transition sets the new state, the timestamp that belongs to it, and, when the new
state retires the candidate, drops its staged schema. Each joins the transaction its
caller already holds, so a rollback undoes all of them together; the caller keeps that
transaction and whatever commits with it: the Import operation's ending, its own audit
entry, the table exchange. The exception is the best-effort ``block_unsaved_preparation``:
it runs after the caller's transaction failed, as its own autocommitted update and drop.
Which transitions are allowed stays with the callers' guards.

This module sits underneath the Candidate lifecycle, review, source retention and the
Import operation wrapper and imports none of them. It reaches no county code (ADR-0018).
"""

from __future__ import annotations

from django.db import DatabaseError
from django.utils import timezone

from counties.common.candidate_staging import drop_candidate_schema
from counties.common.import_states import CandidateState
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


def end_preparation(
    candidate: ImportCandidate, state: CandidateState, *, error: str | None = None
) -> None:
    """Save how preparation ended: prepared, awaiting review, or blocked.

    The caller saves its Import operation first, in the same transaction. A blocked
    candidate stores ``error`` when given and loses its staged schema. A preparation
    interrupted before it decided passes ``CandidateState.PREPARING`` and is saved still
    preparing; the next writer's reconciliation blocks it.
    """
    candidate.state = state
    if error is not None:
        candidate.evidence["error"] = error
    candidate.save()
    if state == CandidateState.BLOCKED:
        # Dropped with the blocked state, so a rollback undoes both.
        drop_candidate_schema(candidate)


def block_unsaved_preparation(candidate: ImportCandidate, error: str) -> None:
    """Best effort after preparation's ending could not be saved: block with ``error`` only.

    Its evidence may be what could not be saved, so only the error is kept. A database
    failure here is ignored: the next writer's reconciliation blocks it and drops its
    schema.
    """
    candidate.state = CandidateState.BLOCKED
    candidate.evidence = {"error": error}
    try:
        ImportCandidate.objects.filter(pk=candidate.pk).update(
            state=candidate.state, evidence=candidate.evidence
        )
        drop_candidate_schema(candidate)
    except DatabaseError:
        pass


def block_unfinished(candidate: ImportCandidate, error: str) -> None:
    """Block a candidate whose preparation will never finish, and drop its staged schema.

    Only a preparing candidate is blocked, with ``error``; one already blocked keeps its
    own error. The schema is dropped either way.
    """
    if candidate.state == CandidateState.PREPARING:
        candidate.state = CandidateState.BLOCKED
        candidate.evidence["error"] = error
        candidate.save(update_fields=["state", "evidence"])
    drop_candidate_schema(candidate)


def decide_review(candidate: ImportCandidate, decision: CandidateState) -> None:
    """Record a reviewer's decision, approved or rejected; a rejection is dated and dropped."""
    candidate.state = decision
    if decision == CandidateState.REJECTED:
        candidate.rejected_at = timezone.now()
    candidate.save(update_fields=["state", "rejected_at"])
    if decision == CandidateState.REJECTED:
        drop_candidate_schema(candidate)


def publish_candidate(candidate: ImportCandidate, operation: ImportOperation) -> None:
    """Supersede the county's previously published candidates, then publish ``candidate``.

    Called inside the county's observed publication transaction, after the cutover.
    """
    now = timezone.now()
    for previous in (
        ImportCandidate.objects.select_for_update()
        .filter(county=candidate.county, state=CandidateState.PUBLISHED)
        .exclude(pk=candidate.pk)
    ):
        previous.state = CandidateState.SUPERSEDED
        previous.superseded_at = now
        previous.save(update_fields=["state", "superseded_at"])
        drop_candidate_schema(previous)
        ImportAuditEntry.objects.create(
            operation=previous.operation,
            kind="supersession",
            actor=operation.actor,
            reason="A newer qualified dataset was published",
            evidence={
                "replacement_candidate_id": str(candidate.pk),
                "operation_id": str(operation.pk),
            },
            result="superseded",
        )
    candidate.state = CandidateState.PUBLISHED
    candidate.published_at = now
    candidate.save(update_fields=["state", "published_at"])
    drop_candidate_schema(candidate)  # cut over, so published data no longer needs it
