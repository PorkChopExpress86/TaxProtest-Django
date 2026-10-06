"""The operator benchmark harness is county-neutral, and production code never imports tests."""

from __future__ import annotations

import ast
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from counties.common.benchmarking import measure, swapped_settings
from counties.common.models import ImportCandidate, ImportOperation

ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_PACKAGES = ("counties", "taxprotest")


def _is_test_path(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_")


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _is_test_module(module: str) -> bool:
    parts = module.split(".")
    return "tests" in parts or any(part.startswith("test_") for part in parts)


class ProductionImportBoundaryTests(SimpleTestCase):
    def test_no_production_module_imports_a_test_module(self):
        offenders = [
            f"{path.relative_to(ROOT).as_posix()}: {module}"
            for package in PRODUCTION_PACKAGES
            for path in sorted((ROOT / package).rglob("*.py"))
            if not _is_test_path(path.relative_to(ROOT))
            for module in sorted(_imported_modules(path))
            if _is_test_module(module)
        ]
        self.assertEqual(offenders, [])


class BenchmarkHarnessNeutralityTests(SimpleTestCase):
    def test_harness_names_no_county_and_imports_no_county_package(self):
        path = ROOT / "counties" / "common" / "benchmarking.py"
        source = path.read_text(encoding="utf-8").lower()

        self.assertNotIn("harris", source)
        self.assertNotIn("brazos", source)
        self.assertEqual(
            {m for m in _imported_modules(path) if m.startswith("counties.")},
            {"counties.common.models"},
        )


class _FakeResult:
    def __init__(self, candidate_id, label):
        self.candidate_id = candidate_id
        self.label = label


class BenchmarkHarnessTests(TransactionTestCase):
    def _candidate(self) -> ImportCandidate:
        operation = ImportOperation.objects.create(county="fake_county", intent="preview")
        candidate = ImportCandidate.objects.create(
            operation=operation, county="fake_county", storage_schema="fake_county_candidate_1"
        )
        with connection.cursor() as cursor:
            cursor.execute('CREATE SCHEMA "fake_county_candidate_1"')
        return candidate

    def _schema_exists(self) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.schemata "
                "WHERE schema_name = 'fake_county_candidate_1'"
            )
            return cursor.fetchone() is not None

    def test_measure_reports_the_run_and_discards_its_candidate(self):
        candidate = self._candidate()

        stats = measure(
            "fake_county",
            rows=4,
            run=lambda: _FakeResult(candidate.pk, "held"),
            status=lambda result: result.label,
        )

        self.assertEqual((stats.county, stats.rows, stats.status), ("fake_county", 4, "held"))
        self.assertGreaterEqual(stats.duration, 0.0)
        self.assertGreaterEqual(stats.peak_memory_mb, 0.0)
        self.assertFalse(ImportCandidate.objects.filter(pk=candidate.pk).exists())
        self.assertFalse(self._schema_exists())

    def test_measure_tolerates_a_run_without_a_candidate(self):
        stats = measure(
            "fake_county", rows=1, run=lambda: _FakeResult(None, "failed"), status=lambda r: r.label
        )

        self.assertEqual(stats.status, "failed")

    def test_swapped_settings_restores_previous_and_absent_values(self):
        with swapped_settings(DEBUG="swapped", BENCHMARK_ONLY_SETTING="set"):
            self.assertEqual(settings.DEBUG, "swapped")
            self.assertEqual(settings.BENCHMARK_ONLY_SETTING, "set")

        self.assertIsNot(settings.DEBUG, "swapped")
        self.assertFalse(hasattr(settings, "BENCHMARK_ONLY_SETTING"))
