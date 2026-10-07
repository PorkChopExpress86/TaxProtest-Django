"""The operator benchmark harness measures a run and discards its candidate."""

from __future__ import annotations

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase

from counties.common.benchmarking import measure, swapped_settings
from counties.common.models import ImportCandidate, ImportOperation


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
