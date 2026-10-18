"""A Candidate that can no longer be reviewed or published keeps no staged schema (#133)."""

from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse

from counties.common.candidate_lifecycle import prepare, publish
from counties.common.import_audit import audited_operation
from counties.common.import_retention import cleanup_sources
from counties.common.import_review import (
    ImportReviewRejected,
    captured_binding,
    checked_binding,
    review_candidate,
)
from counties.common.models import ImportCandidate
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)


def schema_exists(candidate) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
            [candidate.storage_schema],
        )
        return cursor.fetchone()[0]


def leave_schema_behind(candidate):
    """Recreate the schema an earlier release left for a retired candidate."""
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{candidate.storage_schema}"')
        cursor.execute(f'CREATE TABLE "{candidate.storage_schema}".leftover (id int)')


class SchemaRetirementContract:
    """Behaviour every county port must give a retired candidate's staged schema."""

    def publish_candidate(self, candidate):
        with audited_operation(self.county, "apply", actor="operator") as operation:
            publish(operation, candidate.pk, user=self.reviewer, reason="Apply reviewed")
        candidate.refresh_from_db()
        return candidate

    def prepare_fixture(self, *, ready=True, **publication):
        with audited_operation(self.county, "fixture", actor="operator") as operation:
            prepared = prepare(operation, self.fixture_load(operation, ready=ready), **publication)
        prepared.candidate.refresh_from_db()
        return prepared.candidate

    def test_a_candidate_under_review_keeps_its_schema(self):
        self.assertTrue(schema_exists(self.candidate))
        self.approve()
        self.assertTrue(schema_exists(self.candidate))

    def test_published_candidate_schema_is_dropped_after_cutover(self):
        self.approve()
        candidate = self.publish_candidate(self.candidate)
        self.assertEqual(candidate.state, "published")
        self.assertFalse(schema_exists(candidate))
        self.assertEqual(self.publish_candidate(candidate).state, "published")

    def test_superseded_candidate_schema_is_dropped(self):
        self.approve()
        first = self.publish_candidate(self.candidate)
        leave_schema_behind(first)
        self.write_candidate_rows(ready=True)
        second = self.prepare_fixture(automatic_publication=True, reason="Scheduled import")
        self.assertEqual(second.state, "published")
        first.refresh_from_db()
        self.assertEqual(first.state, "superseded")
        self.assertFalse(schema_exists(first))
        self.assertFalse(schema_exists(second))

    def test_rejected_candidate_schema_is_dropped(self):
        review_candidate(
            self.candidate,
            user=self.reviewer,
            reason="Coverage loss is not acceptable",
            decision="rejected",
            expected_binding=captured_binding(self.candidate),
        )
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.state, "rejected")
        self.assertFalse(schema_exists(self.candidate))

    def test_blocked_candidate_schema_is_dropped(self):
        candidate = self.prepare_fixture(ready=False)
        self.assertEqual(candidate.state, "blocked")
        self.assertFalse(schema_exists(candidate))

    def test_failed_load_candidate_schema_is_dropped(self):
        with (
            self.assertRaisesMessage(OSError, "Source unreadable"),
            audited_operation(self.county, "fixture", actor="operator") as operation,
        ):
            prepare(operation, self.fixture_load(operation, failure=OSError("Source unreadable")))
        candidate = ImportCandidate.objects.get(operation=operation)
        self.assertEqual(candidate.state, "blocked")
        self.assertEqual(candidate.evidence["error"], "Source unreadable")
        self.assertFalse(schema_exists(candidate))

    def test_retention_drops_schemas_left_by_retired_candidates_and_repeats_safely(self):
        blocked = self.prepare_fixture(ready=False)
        leave_schema_behind(blocked)
        held = self.prepare_fixture()
        self.assertEqual(held.state, "awaiting_review")

        cleanup = cleanup_sources(self.county, actor="operator", reason="Reclaim space")
        self.assertFalse(schema_exists(blocked))
        self.assertTrue(schema_exists(held))
        self.assertTrue(schema_exists(self.candidate))
        self.assertEqual(cleanup.evidence["dropped_schemas"], [blocked.storage_schema])

        repeated = cleanup_sources(self.county, actor="operator", reason="Reclaim space")
        self.assertEqual(repeated.status, "completed")
        self.assertEqual(repeated.evidence["dropped_schemas"], [])

    def test_retired_candidate_is_refused_review_without_reading_its_schema(self):
        self.approve()
        candidate = self.publish_candidate(self.candidate)
        with self.assertRaisesMessage(ImportReviewRejected, "staged data is retired"):
            checked_binding(candidate)
        self.client.force_login(self.reviewer)
        page = self.client.get(reverse("admin:data_importcandidate_review", args=[candidate.pk]))
        self.assertContains(page, "staged data is retired")


class HarrisSchemaRetirementTests(
    SchemaRetirementContract, HarrisCandidateContract, TransactionTestCase
):
    pass


class BrazosSchemaRetirementTests(
    SchemaRetirementContract, BrazosCandidateContract, TransactionTestCase
):
    pass
