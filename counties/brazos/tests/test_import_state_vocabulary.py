"""Brazos import code uses the shared state vocabulary (ADR-0023)."""

from collections import Counter
from pathlib import Path

from django.test import SimpleTestCase

from counties.common.tests.test_shared_code_boundaries import (
    bare_status_strings,
    names_imported_from,
)

BRAZOS = Path(__file__).resolve().parents[1]
IMPORT_MODULES = (
    "property_import.py",
    "candidate.py",
    "coordinate_enrichment.py",
)

# The one allow-list of literals spelling a status: (module, literal) -> occurrences.
ALLOWED: dict[tuple[str, str], int] = {
    # Not statuses: a persisted evidence key (ADR-0024).
    ("property_import.py", "already_applied"): 1,
    # The workflow state of a preview result; never stored as an operation status.
    ("property_import.py", "validated"): 1,
    # Not statuses: a stored snapshot outcome.
    ("candidate.py", "partial"): 1,
}


class BrazosImportVocabularyTests(SimpleTestCase):
    def test_import_code_reads_the_vocabulary_from_the_shared_module(self):
        for module in IMPORT_MODULES:
            with self.subTest(module=module):
                imported = names_imported_from(
                    (BRAZOS / module).read_text(encoding="utf-8"), "counties.common.import_audit"
                )
                self.assertFalse(imported & {"OperationStatus", "CandidateState"})

    def test_bare_status_strings_are_exactly_the_allow_list(self):
        found = Counter(
            (module, literal)
            for module in IMPORT_MODULES
            for literal in bare_status_strings((BRAZOS / module).read_text(encoding="utf-8"))
        )

        self.assertEqual(dict(found), ALLOWED)
