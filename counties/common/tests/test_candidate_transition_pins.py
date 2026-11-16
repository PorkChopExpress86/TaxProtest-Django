"""Pin how a candidate's state, timestamps and schema survive a failed review or publication.

Today's behaviour, pinned before Candidate state gets one writer (#190, #191). Failures are
injected at the SQL statements that stay put when the state writes move: the staged schema
drop and the final publication audit entry.
"""

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from counties.common.candidate_lifecycle import prepare, publish
from counties.common.candidate_ports import published_identity
from counties.common.import_audit import audited_operation
from counties.common.import_review import ImportReviewRejected, captured_binding, review_candidate
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)
from counties.common.tests.test_candidate_schema_retirement import schema_exists


def stored(candidate) -> dict:
    """What is stored for ``candidate`` now, read afresh, with whether its schema exists."""
    row = ImportCandidate.objects.values(
        "state", "rejected_at", "published_at", "superseded_at", "evidence"
    ).get(pk=candidate.pk)
    return {**row, "schema": schema_exists(candidate)}


def fail_after_schema_drop(execute, sql, params, many, context):
    """Let a staged schema drop run, then fail its transaction."""
    result = execute(sql, params, many, context)
    if sql.startswith("DROP SCHEMA"):
        raise OSError("Failed after dropping the staged schema")
    return result


def fail_publication_audit(execute, sql, params, many, context):
    """Fail the publication audit entry, which follows the exchange and every state write."""
    if sql.startswith('INSERT INTO "data_importauditentry"') and "publication" in params:
        raise OSError("Final publication audit failed")
    return execute(sql, params, many, context)


class CandidateTransitionPins:
    """Behaviour every county port gives a failed rejection or publication, and a blocked reject."""

    def reject(self, candidate):
        return review_candidate(
            candidate,
            user=self.reviewer,
            reason="Coverage loss is not acceptable",
            decision="rejected",
            expected_binding=captured_binding(candidate),
        )

    def publish_candidate(self, candidate):
        with audited_operation(self.county, "apply", actor="operator") as operation:
            publish(operation, candidate.pk, user=self.reviewer, reason="Apply reviewed")
        candidate.refresh_from_db()
        return candidate

    def prepare_fixture(self, *, ready=True):
        with audited_operation(self.county, "fixture", actor="operator") as operation:
            prepared = prepare(operation, self.fixture_load(operation, ready=ready))
        prepared.candidate.refresh_from_db()
        return prepared.candidate

    def test_a_rolled_back_rejection_leaves_the_candidate_and_its_schema_as_they_were(self):
        before = stored(self.candidate)
        self.assertEqual(before["state"], "awaiting_review")
        self.assertIsNone(before["rejected_at"])
        self.assertTrue(before["schema"])

        with (
            connection.execute_wrapper(fail_after_schema_drop),
            self.assertRaisesMessage(ImportReviewRejected, "Failed after dropping"),
        ):
            self.reject(self.candidate)

        self.assertEqual(stored(self.candidate), before)
        # Only the refusal is recorded: the decision's own entry rolled back with it.
        (refusal,) = ImportAuditEntry.objects.filter(kind="coverage_review")
        self.assertEqual(refusal.result, "rejected")
        self.assertEqual(refusal.evidence["rejection"], "Failed after dropping the staged schema")
        self.assertEqual(ImportOperation.objects.get(intent="coverage_review").status, "failed")

        # The reviewer can retry the same decision.
        self.reject(self.candidate)
        retried = stored(self.candidate)
        self.assertEqual(retried["state"], "rejected")
        self.assertIsNotNone(retried["rejected_at"])
        self.assertFalse(retried["schema"])

    def test_a_rolled_back_publication_leaves_both_candidates_and_published_data_as_they_were(
        self,
    ):
        self.approve()
        first = self.publish_candidate(self.candidate)
        self.write_candidate_rows(ready=True)
        second = self.prepare_fixture()
        self.assertEqual(second.state, "prepared")
        first_before, second_before = stored(first), stored(second)
        self.assertEqual(first_before["state"], "published")
        self.assertIsNotNone(first_before["published_at"])
        self.assertIsNone(first_before["superseded_at"])
        self.assertIsNone(second_before["published_at"])
        self.assertTrue(second_before["schema"])
        identity, keys = published_identity(self.county), self.published_keys()

        with connection.execute_wrapper(fail_publication_audit), self.assertRaises(OSError):
            self.publish_candidate(second)

        self.assertEqual(stored(first), first_before)
        self.assertEqual(stored(second), second_before)
        self.assertEqual(published_identity(self.county), identity)
        self.assertEqual(self.published_keys(), keys)
        self.assertFalse(ImportAuditEntry.objects.filter(kind="supersession").exists())
        self.assertEqual(
            ImportAuditEntry.objects.filter(kind="publication").count(), 1
        )  # the first candidate's only

        # The candidate can be applied again.
        self.assertEqual(self.publish_candidate(second).state, "published")
        self.assertEqual(stored(first)["state"], "superseded")

    def test_a_reviewer_can_reject_a_blocked_candidate_and_its_rejection_is_dated(self):
        blocked = self.prepare_fixture(ready=False)
        before = stored(blocked)
        self.assertEqual(before["state"], "blocked")
        self.assertIsNone(before["rejected_at"])
        self.assertFalse(before["schema"])

        started = timezone.now()
        self.reject(blocked)
        finished = timezone.now()

        after = stored(blocked)
        self.assertEqual(after["state"], "rejected")
        self.assertTrue(started <= after["rejected_at"] <= finished)
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertFalse(after["schema"])
        (decision,) = ImportAuditEntry.objects.filter(
            kind="coverage_review", operation=blocked.operation
        )
        self.assertEqual(decision.result, "rejected")
        self.assertEqual(decision.reason, "Coverage loss is not acceptable")
        self.assertNotIn("rejection", decision.evidence)


class HarrisCandidateTransitionPins(
    CandidateTransitionPins, HarrisCandidateContract, TransactionTestCase
):
    pass


class BrazosCandidateTransitionPins(
    CandidateTransitionPins, BrazosCandidateContract, TransactionTestCase
):
    pass
