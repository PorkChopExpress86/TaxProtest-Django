"""Preparation behaves identically through the shared Candidate lifecycle for every county."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from django.db import DataError
from django.test import TransactionTestCase

from counties.common.candidate_lifecycle import prepare, publish
from counties.common.candidate_ports import published_identity
from counties.common.import_audit import audited_operation
from counties.common.models import ImportCandidate, ImportOperation
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)


class PreparationContract:
    """Behaviour every county port must give preparation, with the load step swapped."""

    def prepare_fixture(self, *, ready=True, **publication):
        with audited_operation(self.county, "fixture", actor="operator") as operation:
            prepared = prepare(operation, self.fixture_load(operation, ready=ready), **publication)
        operation.refresh_from_db()
        prepared.candidate.refresh_from_db()
        return operation, prepared.candidate

    def publish_setup_candidate(self):
        self.approve()
        with audited_operation(self.county, "apply", actor="operator") as operation:
            publish(operation, self.candidate.pk, user=self.reviewer, reason="Apply reviewed")

    def test_a_candidate_whose_ending_cannot_be_saved_is_not_left_preparing(self):
        real_save = ImportCandidate.save

        def save(candidate, *args, **kwargs):
            if kwargs.get("force_insert"):
                return real_save(candidate, *args, **kwargs)
            raise DataError("Candidate evidence is too large")

        with (
            self.assertRaisesMessage(DataError, "too large"),
            audited_operation(self.county, "fixture", actor="operator") as operation,
            patch.object(ImportCandidate, "save", save),
        ):
            prepare(operation, self.fixture_load(operation))
        operation.refresh_from_db()
        candidate = ImportCandidate.objects.get(operation=operation)
        self.assertEqual(operation.status, "failed")
        self.assertIsNotNone(operation.finished_at)
        self.assertEqual(candidate.state, "blocked")
        self.assertIn("too large", candidate.evidence["error"])

    def test_only_a_load_that_keeps_published_rows_is_seeded_with_them(self):
        for replaces_published, staged in ((False, ["PUBLISHED"]), (True, [])):
            with self.subTest(replaces_published=replaces_published):
                seen = []
                with audited_operation(self.county, "fixture", actor="operator") as operation:
                    load = self.fixture_load(operation)

                    def run(candidate, operation, write=load.run, seen=seen):
                        seen.extend(self.published_keys())
                        return write(candidate, operation)

                    prepare(
                        operation,
                        replace(load, run=run, replaces_published=replaces_published),
                    )
                self.assertEqual(seen, staged)
                self.assertEqual(self.published_keys(), ["PUBLISHED"])

    def test_first_import_awaits_review(self):
        before = published_identity(self.county)
        operation, candidate = self.prepare_fixture()
        self.assertEqual(candidate.state, "awaiting_review")
        self.assertEqual(operation.status, "awaiting_review")
        self.assertEqual(operation.evidence["candidate_id"], str(candidate.pk))
        self.assertEqual(candidate.baseline, before)
        self.assertEqual(candidate.evidence["fixture"], "rows written")
        self.assertTrue(candidate.evidence["coverage"]["requires_review"])
        self.assertEqual(
            [Path(source["path"]).name for source in candidate.sources], ["fixture-source.txt"]
        )
        self.assertEqual(published_identity(self.county), before)
        self.assertEqual(self.published_keys(), ["PUBLISHED"])

    def test_hard_coverage_failure_is_blocked(self):
        operation, candidate = self.prepare_fixture(ready=False)
        self.assertEqual(candidate.state, "blocked")
        self.assertEqual(operation.status, "blocked")
        self.assertIn(
            "search: zero eligible records for a supported outcome",
            candidate.evidence["coverage"]["hard_failures"],
        )
        self.assertEqual(self.published_keys(), ["PUBLISHED"])

    def test_within_threshold_candidate_is_prepared_and_a_preview_never_applies(self):
        self.write_candidate_rows(ready=True)
        before = published_identity(self.county)
        operation, candidate = self.prepare_fixture()
        self.assertEqual(candidate.state, "prepared")
        self.assertEqual(operation.status, "prepared")
        self.assertTrue(candidate.evidence["coverage"]["automatic_publication_allowed"])
        self.assertEqual(published_identity(self.county), before)

    def test_within_threshold_candidate_publishes_automatically_when_asked(self):
        self.write_candidate_rows(ready=True)
        operation, candidate = self.prepare_fixture(
            automatic_publication=True, reason="Scheduled import"
        )
        self.assertEqual(candidate.state, "published")
        self.assertEqual(operation.status, "published")
        self.assertEqual(
            operation.publication_after,
            published_identity(self.county) | {"candidate_id": str(candidate.pk)},
        )

    def test_load_failure_blocks_with_error_sources_and_evidence_saved(self):
        self.publish_setup_candidate()
        published = published_identity(self.county)
        inherited = {source["path"] for source in self.candidate.sources}
        with (
            self.assertRaisesMessage(OSError, "Source unreadable"),
            audited_operation(self.county, "fixture", actor="operator") as operation,
        ):
            load = replace(
                self.fixture_load(operation, failure=OSError("Source unreadable")),
                carries_published=True,
            )
            prepare(operation, load)
        self.assertEqual(published_identity(self.county), published)
        operation = ImportOperation.objects.get(intent="fixture")
        candidate = ImportCandidate.objects.get(operation=operation)
        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.evidence["candidate_id"], str(candidate.pk))
        self.assertEqual(operation.evidence["validation"], {"valid": True})
        self.assertEqual(candidate.state, "blocked")
        self.assertEqual(candidate.evidence["error"], "Source unreadable")
        self.assertEqual(candidate.request, dict(load.identity))
        self.assertEqual(
            {source["path"] for source in candidate.sources},
            inherited | {str(self.root / "fixture-source.txt")},
        )
        self.assertEqual(
            {
                source["path"]
                for source in candidate.sources
                if source.get("reference") == "inherited"
            },
            inherited,
        )


class HarrisPreparationContractTests(
    PreparationContract, HarrisCandidateContract, TransactionTestCase
):
    pass


class BrazosPreparationContractTests(
    PreparationContract, BrazosCandidateContract, TransactionTestCase
):
    pass
