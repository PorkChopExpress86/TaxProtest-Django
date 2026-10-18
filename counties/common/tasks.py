"""Run operator-requested Candidate lifecycle calls in the Celery worker, not the web request.

Each task loads its rows by id and makes exactly the lifecycle call the admin view used to
make. The lifecycle opens its own Import operation, takes the county writer or is rejected
(ADR-0011), rechecks authorization and evidence, and records a failure on that operation, so
the task adds no status or retry of its own. Recovery prepares a new candidate each run, so
these tasks are never retried or acknowledged late.
"""

from celery import shared_task
from django.contrib.auth import get_user_model

from counties.common import candidate_lifecycle, import_review
from counties.common.models import ImportCandidate


def _operator(user_id: int):
    """The requesting user while still active staff; the lifecycle rechecks permissions."""
    return get_user_model().objects.get(pk=user_id, is_active=True, is_staff=True)


@shared_task
def apply_candidate(candidate_id: str, user_id: int, reason: str) -> str:
    operation = candidate_lifecycle.apply(
        ImportCandidate.objects.get(pk=candidate_id), user=_operator(user_id), reason=reason
    )
    return str(operation.pk)


@shared_task
def review_candidate(
    candidate_id: str, user_id: int, reason: str, decision: str, expected_binding: str
) -> None:
    import_review.review_candidate(
        ImportCandidate.objects.get(pk=candidate_id),
        user=_operator(user_id),
        reason=reason,
        decision=decision,
        expected_binding=expected_binding,
    )


@shared_task
def recover_dataset(candidate_id: str, user_id: int, reason: str, binding: str) -> str:
    operation = candidate_lifecycle.recover(
        ImportCandidate.objects.get(pk=candidate_id),
        user=_operator(user_id),
        reason=reason,
        binding=binding,
    )
    return str(operation.pk)
