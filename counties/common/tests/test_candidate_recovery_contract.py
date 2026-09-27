"""Recovery replays retained sources through the shared Candidate lifecycle for every county."""

from pathlib import Path

from django.test import TransactionTestCase

from counties.common.candidate_lifecycle import apply, recover
from counties.common.candidate_ports import published_identity
from counties.common.import_recovery import ReplayRejected, replay_binding
from counties.common.models import ImportOperation
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)


def digests(candidate):
    return {source["sha256"] for source in candidate.sources}


class RecoveryContract:
    """Behaviour every county port must give recovery (ADR-0016)."""

    def setUp(self):
        super().setUp()
        self.approve()
        apply(self.candidate, user=self.reviewer, reason="Publish first import")
        self.candidate.refresh_from_db()
        self.published = published_identity(self.county)

    def recover(self):
        return recover(
            self.candidate,
            user=self.reviewer,
            reason="Restore retained sources",
            binding=replay_binding(self.candidate),
        )

    def test_recovery_replays_retained_sources_through_ordinary_rules(self):
        operation = self.recover()
        recovered = operation.candidate
        self.assertEqual(operation.origin, "admin_recovery")
        self.assertEqual(
            operation.evidence["recovery"]["source_candidate_id"], str(self.candidate.pk)
        )
        self.assertEqual(digests(recovered), digests(self.candidate))
        self.assertTrue(operation.evidence["validation"]["valid"])
        self.assertEqual(recovered.baseline, self.published)
        search = recovered.evidence["coverage"]["outcomes"]["search"]
        self.assertEqual(search["prior_ready"], search["total_ready"])
        self.assertEqual(search["unexplained_loss"], 0)
        self.assertEqual(recovered.state, "prepared")
        self.assertEqual(operation.status, "prepared")
        self.assertEqual(published_identity(self.county), self.published)
        for target, kind in (
            (operation, "dataset_recovery"),
            (self.candidate.operation, "recovery_request"),
        ):
            self.assertTrue(target.audit_entries.filter(kind=kind, result="prepared").exists())

        applied = apply(recovered, user=self.reviewer, reason="Apply restored dataset")
        recovered.refresh_from_db()
        self.candidate.refresh_from_db()
        self.assertEqual(applied.status, "published")
        self.assertEqual(recovered.state, "published")
        self.assertEqual(self.candidate.state, "superseded")

    def assert_rejected(self, message):
        with self.assertRaisesMessage(ReplayRejected, message):
            self.recover()
        self.assertEqual(ImportOperation.objects.get(origin="admin_recovery").status, "failed")
        self.assertTrue(
            self.candidate.operation.audit_entries.filter(
                kind="recovery_request", result="failed"
            ).exists()
        )
        self.assertEqual(published_identity(self.county), self.published)

    def test_changed_retained_source_is_rejected(self):
        Path(self.candidate.sources[0]["path"]).write_text("changed")
        self.assert_rejected("Retained source changed")

    def test_missing_retained_source_is_rejected(self):
        Path(self.candidate.sources[0]["path"]).unlink()
        self.assert_rejected("Retained source unavailable")


class HarrisRecoveryContractTests(RecoveryContract, HarrisCandidateContract, TransactionTestCase):
    pass


class BrazosRecoveryContractTests(RecoveryContract, BrazosCandidateContract, TransactionTestCase):
    pass
