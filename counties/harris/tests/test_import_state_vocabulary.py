"""Harris import code uses the shared state vocabulary and pairs it explicitly (ADR-0023)."""

from collections import Counter
from pathlib import Path

from django.test import SimpleTestCase

from counties.common.import_states import CandidateState, OperationStatus
from counties.common.tests.test_shared_code_boundaries import (
    bare_status_strings,
    names_imported_from,
)
from counties.harris.etl_pipeline.orchestrator import (
    HARRIS_STATUS_FOR_HELD_CANDIDATE,
    OPERATION_STATUS_FOR_HARRIS_STATUS,
    HarrisImportStatus,
)

HARRIS = Path(__file__).resolve().parents[1]
IMPORT_MODULES = (
    "etl_pipeline/orchestrator.py",
    "etl_pipeline/candidate.py",
    "readiness.py",
)

# The one allow-list of literals spelling a status: (module, literal) -> occurrences.
ALLOWED: dict[tuple[str, str], int] = {
    # The Harris import status definition, a county result vocabulary (ADR-0023).
    ("etl_pipeline/orchestrator.py", "completed"): 1,
    ("etl_pipeline/orchestrator.py", "partial"): 1,
    ("etl_pipeline/orchestrator.py", "prepared"): 1,
    ("etl_pipeline/orchestrator.py", "awaiting_review"): 1,
    ("etl_pipeline/orchestrator.py", "blocked"): 1,
    # One definition member; the rest are per-source load metric keys, not statuses.
    ("etl_pipeline/orchestrator.py", "failed"): 8,
    # Not statuses: a persisted evidence key (ADR-0024).
    ("etl_pipeline/orchestrator.py", "already_applied"): 2,
}


class HarrisImportStatusTests(SimpleTestCase):
    def test_members_and_serialized_values_are_unchanged(self):
        self.assertEqual(
            {status.name: status.value for status in HarrisImportStatus},
            {
                "COMPLETED": "completed",
                "FAILED": "failed",
                "PARTIAL": "partial",
                "PREPARED": "prepared",
                "AWAITING_REVIEW": "awaiting_review",
                "BLOCKED": "blocked",
            },
        )

    def test_every_harris_status_pairs_with_the_operation_status_it_stores(self):
        self.assertEqual(
            {
                status: (type(paired), paired.value)
                for status, paired in OPERATION_STATUS_FOR_HARRIS_STATUS.items()
            },
            {
                HarrisImportStatus.COMPLETED: (OperationStatus, "completed"),
                HarrisImportStatus.FAILED: (OperationStatus, "failed"),
                HarrisImportStatus.PARTIAL: (OperationStatus, "partial"),
                HarrisImportStatus.PREPARED: (OperationStatus, "prepared"),
                HarrisImportStatus.AWAITING_REVIEW: (OperationStatus, "awaiting_review"),
                HarrisImportStatus.BLOCKED: (OperationStatus, "blocked"),
            },
        )

    def test_only_held_candidate_states_pair_with_a_harris_status(self):
        self.assertEqual(
            HARRIS_STATUS_FOR_HELD_CANDIDATE,
            {
                CandidateState.PREPARED: HarrisImportStatus.PREPARED,
                CandidateState.AWAITING_REVIEW: HarrisImportStatus.AWAITING_REVIEW,
                CandidateState.BLOCKED: HarrisImportStatus.BLOCKED,
            },
        )
        self.assertTrue(
            all(type(state) is CandidateState for state in HARRIS_STATUS_FOR_HELD_CANDIDATE)
        )


class HarrisImportVocabularyTests(SimpleTestCase):
    def test_import_code_reads_the_vocabulary_from_the_shared_module(self):
        for module in IMPORT_MODULES:
            with self.subTest(module=module):
                imported = names_imported_from(
                    (HARRIS / module).read_text(encoding="utf-8"), "counties.common.import_audit"
                )
                self.assertFalse(imported & {"OperationStatus", "CandidateState"})

    def test_bare_status_strings_are_exactly_the_allow_list(self):
        found = Counter(
            (module, literal)
            for module in IMPORT_MODULES
            for literal in bare_status_strings((HARRIS / module).read_text(encoding="utf-8"))
        )

        self.assertEqual(dict(found), ALLOWED)
