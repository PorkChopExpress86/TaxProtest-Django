"""Admin apply, review and recovery hand the Candidate lifecycle to Celery (issue #141)."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from counties.common import candidate_lifecycle, import_review, tasks
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


class AdminLifecycleTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("operator", password="test")
        self.operation = ImportOperation.objects.create(county="brazos", intent="import")
        self.candidate = ImportCandidate.objects.create(
            county="brazos", operation=self.operation, storage_schema="brazos_candidate_test"
        )
        self.operation.evidence = {"candidate_id": str(self.candidate.pk)}
        self.operation.save()
        self.client.force_login(self.user)

    def test_each_view_enqueues_and_returns_without_running_the_lifecycle(self):
        pk, user = str(self.candidate.pk), self.user.pk
        cases = (
            (
                reverse("admin:data_importcandidate_apply", args=[pk]),
                {"reason": "Publish"},
                tasks.apply_candidate,
                {"candidate_id": pk, "user_id": user, "reason": "Publish"},
            ),
            (
                reverse("admin:data_importcandidate_review", args=[pk]),
                {"decision": "approved", "reason": "Reviewed", "binding": "b"},
                tasks.review_candidate,
                {
                    "candidate_id": pk,
                    "user_id": user,
                    "reason": "Reviewed",
                    "decision": "approved",
                    "expected_binding": "b",
                },
            ),
            (
                reverse("admin:data_importoperation_recover_dataset", args=[self.operation.pk]),
                {"reason": "Restore", "binding": "b"},
                tasks.recover_dataset,
                {"candidate_id": pk, "user_id": user, "reason": "Restore", "binding": "b"},
            ),
        )
        for url, data, task, expected in cases:
            with self.subTest(task=task.name), patch.object(task, "delay") as delay:
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    response["Location"],
                    reverse("admin:data_importoperation_changelist")
                    + "?county__exact=brazos&actor__exact=operator",
                )
                delay.assert_called_once_with(**expected)
        # Every lifecycle call opens its own Import operation first; only the fixture's exists.
        self.assertEqual(list(ImportOperation.objects.all()), [self.operation])
        self.assertFalse(ImportAuditEntry.objects.exists())

    def test_each_task_makes_the_lifecycle_call_the_view_made(self):
        pk = str(self.candidate.pk)
        with patch.object(candidate_lifecycle, "apply") as apply:
            tasks.apply_candidate(candidate_id=pk, user_id=self.user.pk, reason="Publish")
        apply.assert_called_once_with(self.candidate, user=self.user, reason="Publish")
        with patch.object(import_review, "review_candidate") as review:
            tasks.review_candidate(
                candidate_id=pk,
                user_id=self.user.pk,
                reason="Reviewed",
                decision="approved",
                expected_binding="b",
            )
        review.assert_called_once_with(
            self.candidate,
            user=self.user,
            reason="Reviewed",
            decision="approved",
            expected_binding="b",
        )
        with patch.object(candidate_lifecycle, "recover") as recover:
            tasks.recover_dataset(
                candidate_id=pk, user_id=self.user.pk, reason="Restore", binding="b"
            )
        recover.assert_called_once_with(
            self.candidate, user=self.user, reason="Restore", binding="b"
        )

    def test_a_deactivated_operator_starts_no_lifecycle_call(self):
        self.user.is_active = False
        self.user.save()
        with (
            patch.object(candidate_lifecycle, "apply") as apply,
            self.assertRaises(get_user_model().DoesNotExist),
        ):
            tasks.apply_candidate(
                candidate_id=str(self.candidate.pk), user_id=self.user.pk, reason="Publish"
            )
        apply.assert_not_called()

    def test_review_page_shows_recorded_refusals_without_live_rechecks(self):
        url = reverse("admin:data_importcandidate_review", args=[self.candidate.pk])
        self.operation.evidence["validation"] = {"valid": True}
        self.operation.save()
        self.candidate.state = "awaiting_review"
        self.candidate.sources = [{"path": "/nonexistent/source.txt", "sha256": "0" * 64}]
        self.candidate.evidence = {"coverage": {"hard_failures": ["zero eligible properties"]}}
        self.candidate.save()
        self.assertContains(self.client.get(url), "zero eligible properties")

        # Source files, the published baseline and staged contents are rechecked when the
        # decision runs in the worker, not while the page loads.
        self.candidate.evidence = {"coverage": {"hard_failures": [], "requires_review": True}}
        self.candidate.save()
        with patch.object(import_review, "published_identity") as baseline:
            page = self.client.get(url)
        self.assertNotContains(page, "Approval unavailable")
        baseline.assert_not_called()
