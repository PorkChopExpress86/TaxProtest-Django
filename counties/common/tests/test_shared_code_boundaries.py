"""Boundary guard: shared lifecycle code names no county and production code imports no tests.

It also requires recorded import sources to come in a deterministic order: a directory
listing passed to ``record_sources`` goes through ``sorted``, because the recorded order
feeds the hashed bindings pinned as persisted evidence (ADR-0024).

Shared Import operation, candidate lifecycle, review, recovery, retention and writer code
reads county facts only through the County registration (ADR-0018, ADR-0022). This one
module scans those files for county names, county imports and bare candidate-state or
operation-status strings, against one exact allow-list.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

from django.test import SimpleTestCase

from counties.common.county_registry import registered_slugs
from counties.common.import_states import CandidateState, LegacyOperationStatus, OperationStatus
from counties.common.tests.fake_county import FAKE_COUNTY

ROOT = Path(__file__).resolve().parents[3]
COMMON = ROOT / "counties" / "common"
PRODUCTION_PACKAGES = ("counties", "taxprotest")

# check_deployment_readiness.py is not scanned: its core-table list stays static by
# decision until a third county exists (ADR-0022).
SHARED_MODULES = (
    *sorted(COMMON.glob("import_*.py")),
    *sorted(COMMON.glob("candidate_*.py")),
    COMMON / "county_registry.py",
    COMMON / "admin.py",
    COMMON / "benchmarking.py",
    COMMON / "management" / "commands" / "cleanup_import_sources.py",
)

# Source abbreviations used for county identifiers; the fake third county's slug is
# included so no shared file can be edited to make the fake county work.
COUNTY_ALIASES = ("hcad", "bcad")

VOCABULARIES = (OperationStatus, LegacyOperationStatus, CandidateState)
STATUS_VOCABULARY = frozenset(member.value for vocabulary in VOCABULARIES for member in vocabulary)
VOCABULARY_CLASSES = frozenset(vocabulary.__name__ for vocabulary in VOCABULARIES)

# The one allow-list: (module, literal) -> occurrences. It must match exactly, so a new
# leak fails and so does a stale entry once its literal is removed.
ALLOWED: dict[tuple[str, str], int] = {
    # Not statuses: the candidate port's question names.
    ("county_registry.py", "published"): 1,
    # Not statuses: a persisted evidence key (ADR-0024), written and read.
    ("candidate_lifecycle.py", "already_applied"): 1,
    ("import_disposition.py", "already_applied"): 1,
    # Not statuses: ImportAuditEntry.result values of publication, supersession, a
    # rejected review attempt, writer recovery and source cleanup, a separate vocabulary.
    ("candidate_lifecycle.py", "published"): 1,
    ("import_recovery.py", "published"): 1,
    ("import_retention.py", "superseded"): 1,
    ("import_retention.py", "running"): 1,
    ("import_retention.py", "completed"): 1,
    ("import_retention.py", "completed_with_warnings"): 1,
    ("import_review.py", "rejected"): 1,
    ("import_writers.py", "rejected"): 1,
    # Not statuses: a per-source cleanup result.
    ("import_retention.py", "failed"): 2,
}


def _relative(path: Path) -> str:
    return path.relative_to(COMMON).as_posix()


def county_name_pattern() -> re.Pattern[str]:
    names = (*registered_slugs(), *COUNTY_ALIASES, FAKE_COUNTY)
    return re.compile("|".join(re.escape(name) for name in names), re.IGNORECASE)


def county_names(source: str) -> list[str]:
    """Every county name in the source text, including comments and identifiers."""
    return county_name_pattern().findall(source)


def imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def names_imported_from(source: str, module: str) -> set[str]:
    """Names a ``from <module> import ...`` statement brings in, wherever it appears."""
    return {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def county_imports(source: str) -> list[str]:
    """Imports of any ``counties`` package other than ``counties.common``."""
    return sorted(
        module
        for module in imported_modules(source)
        if module.split(".")[0] == "counties" and module.split(".")[:2] != ["counties", "common"]
    )


def bare_status_strings(source: str) -> list[str]:
    """String literals spelling a status, outside the shared vocabulary definitions."""
    tree = ast.parse(source)
    vocabulary_definition = {
        id(node)
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef) and cls.name in VOCABULARY_CLASSES
        for node in ast.walk(cls)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in STATUS_VOCABULARY
        and id(node) not in vocabulary_definition
    ]


DIRECTORY_LISTINGS = frozenset({"glob", "rglob", "iterdir", "listdir", "scandir", "walk"})


def _unsorted_listing(expression: ast.AST) -> bool:
    """True when the expression holds a directory listing that no ``sorted(...)`` wraps."""
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "sorted"
    ):
        return False
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr in DIRECTORY_LISTINGS
    ):
        return True
    return any(_unsorted_listing(child) for child in ast.iter_child_nodes(expression))


def unsorted_recorded_listings(source: str) -> list[int]:
    """Lines of ``record_sources`` calls whose paths are an unsorted directory listing.

    A paths argument named by a local variable is followed to that variable's
    assignments in the same function.
    """
    lines: list[int] = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue
        assigned: dict[str, list[ast.AST]] = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, []).append(node.value)
        for call in ast.walk(function):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "record_sources"
            ):
                continue
            paths = call.args[1] if len(call.args) > 1 else None
            for keyword in call.keywords:
                if keyword.arg == "paths":
                    paths = keyword.value
            if paths is None:
                continue
            candidates = assigned.get(paths.id, []) if isinstance(paths, ast.Name) else [paths]
            if any(_unsorted_listing(candidate) for candidate in candidates):
                lines.append(call.lineno)
    return sorted(set(lines))


def _is_test_path(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_")


def _is_test_module(module: str) -> bool:
    parts = module.split(".")
    return "tests" in parts or any(part.startswith("test_") for part in parts)


class BoundaryScannerTests(SimpleTestCase):
    """The scanners catch the leaks the guard exists to stop."""

    def test_county_names_are_found_in_literals_comments_and_identifiers(self):
        source = 'LOCK = {"harris": 1}  # Brazos too\nbcad_root = None\ntravis_key = 2\n'

        self.assertEqual(county_names(source), ["harris", "Brazos", "bcad", "travis"])

    def test_county_imports_are_found_wherever_they_appear(self):
        source = (
            "from counties.common.models import ImportOperation\n"
            "import counties.harris.models\n"
            "def run():\n"
            "    from counties.brazos.candidate import BrazosCandidatePort\n"
        )

        self.assertEqual(
            county_imports(source), ["counties.brazos.candidate", "counties.harris.models"]
        )

    def test_bare_status_strings_are_found_outside_the_vocabulary_definition(self):
        source = (
            "class OperationStatus(StrEnum):\n"
            '    FAILED = "failed"\n'
            "class CandidateState(StrEnum):\n"
            '    PREPARED = "prepared"\n'
            "class LegacyOperationStatus(StrEnum):\n"
            '    VALIDATED = "validated"\n'
            'candidate.state = "awaiting_review"\n'
            'filter(status__in=("validated", OperationStatus.FAILED))\n'
            'reason = "awaiting review is fine as prose"\n'
        )

        self.assertEqual(bare_status_strings(source), ["awaiting_review", "validated"])

    def test_unsorted_directory_listings_passed_to_record_sources_are_found(self):
        source = (
            "def unsorted(operation, root):\n"
            '    record_sources(operation, list(root.glob("*")))\n'
            "def unsorted_variable(operation, layer):\n"
            '    paths = list(layer.rglob("*"))\n'
            "    record_sources(operation, paths, source_id=1)\n"
            "def unsorted_keyword(operation, root):\n"
            "    record_sources(operation, paths=[*root.iterdir()])\n"
            "def sorted_listing(operation, root):\n"
            '    record_sources(operation, sorted(root.glob("*.txt")))\n'
            "def explicit_list(operation, archive, paths):\n"
            "    record_sources(operation, [archive, *paths])\n"
        )

        self.assertEqual(unsorted_recorded_listings(source), [2, 5, 7])


class SharedLifecycleBoundaryTests(SimpleTestCase):
    def test_shared_lifecycle_modules_name_no_county(self):
        for module in SHARED_MODULES:
            with self.subTest(module=_relative(module)):
                self.assertEqual(county_names(module.read_text(encoding="utf-8")), [])

    def test_shared_lifecycle_modules_import_no_county_package(self):
        for module in SHARED_MODULES:
            with self.subTest(module=_relative(module)):
                self.assertEqual(county_imports(module.read_text(encoding="utf-8")), [])

    def test_bare_status_strings_are_exactly_the_allow_list(self):
        found = Counter(
            (_relative(module), literal)
            for module in SHARED_MODULES
            for literal in bare_status_strings(module.read_text(encoding="utf-8"))
        )

        self.assertEqual(dict(found), ALLOWED)


class ProductionImportBoundaryTests(SimpleTestCase):
    def test_no_production_module_imports_a_test_module(self):
        offenders = [
            f"{path.relative_to(ROOT).as_posix()}: {module}"
            for package in PRODUCTION_PACKAGES
            for path in sorted((ROOT / package).rglob("*.py"))
            if not _is_test_path(path.relative_to(ROOT))
            for module in sorted(imported_modules(path.read_text(encoding="utf-8")))
            if _is_test_module(module)
        ]
        self.assertEqual(offenders, [])


class RecordedSourceOrderTests(SimpleTestCase):
    def test_recorded_directory_listings_are_sorted(self):
        offenders = [
            f"{path.relative_to(ROOT).as_posix()}:{line}"
            for package in PRODUCTION_PACKAGES
            for path in sorted((ROOT / package).rglob("*.py"))
            if not _is_test_path(path.relative_to(ROOT))
            for line in unsorted_recorded_listings(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])
