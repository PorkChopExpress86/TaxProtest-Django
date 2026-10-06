"""Characterize what Harris import commands and the Celery task report for real runs.

These pins hold today's exit behaviour, printed text (with its style) and serialized
Celery result keys before adapters classify through the Import disposition (ADR-0023).
"""

import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase

from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_property_source,
)
from counties.harris.etl_pipeline.tests.test_import_publication import harris_request
from counties.harris.models import BuildingDetail, PropertyRecord
from counties.harris.tasks_new import run_etl_pipeline

REUSE = ("--skip-download", "--skip-extract")
VALIDATION_GAP = "Completeness validation failed: Quality Code: only 0.0% populated (1 missing)"
RESULT_KEYS = {
    "status",
    "started_at",
    "completed_at",
    "duration_seconds",
    "stages",
    "errors",
    "warnings",
    "operation_id",
    "candidate_id",
    "wrote_data",
    "already_applied",
}
STAGE_KEYS = {"success", "duration", "error", "metrics"}


def success(text):
    return f"\x1b[32;1m{text}\x1b[0m"


def warning(text):
    return f"\x1b[33;1m{text}\x1b[0m"


def error(text):
    return f"\x1b[31;1m{text}\x1b[0m"


class HarrisImportResultTestCase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(self.settings(**_runtime_settings(self.root)))

    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def write_full_sources(self):
        harris_request(self.root)

    def write_property_source(self, **kwargs):
        _write_property_source(self.root, **kwargs)
        return str(Path(self.root) / "extracted/Real_acct_owner/real_acct.txt")

    def publish_baseline(self, *accounts):
        for account in accounts:
            prop = PropertyRecord.objects.create(
                account_number=account,
                address="Old address",
                is_residential=True,
                is_data_ready=True,
                latitude=29.7,
                longitude=-95.4,
            )
            BuildingDetail.objects.create(
                property=prop,
                account_number=account,
                building_number=1,
                bedrooms=3,
                bathrooms=2,
                heat_area=1800,
            )

    def call(self, *args):
        output = StringIO()
        call_command(*args, stdout=output, force_color=True)
        return output.getvalue().splitlines()

    def call_failing(self, exception, message, *args):
        output = StringIO()
        with self.assertRaises(exception) as raised:
            call_command(*args, stdout=output, force_color=True)
        self.assertEqual(str(raised.exception), message)
        return raised.exception, output.getvalue().splitlines()

    def finished(self, status, state):
        """The one recorded operation and its candidate, with their persisted states."""
        operation = ImportOperation.objects.get()
        candidate = ImportCandidate.objects.get(pk=operation.evidence["candidate_id"])
        self.assertEqual(operation.status, status)
        self.assertEqual(candidate.state, state)
        return operation, candidate

    @staticmethod
    def held(held_as, operation, candidate):
        return (
            f"Harris import {held_as}; published data unchanged. "
            f"Candidate {candidate.pk}; operation {operation.pk}. "
            "Review in Django admin /admin/data/importcandidate/."
        )

    def assert_no_held_notice(self, lines):
        self.assertFalse([line for line in lines if "published data unchanged" in line.lower()])


class ImportAllDataResultTests(HarrisImportResultTestCase):
    def test_first_import_is_held_for_review_and_exits_zero(self):
        self.write_full_sources()

        lines = self.call("import_all_data", *REUSE, "--skip-contract-validation")

        operation, candidate = self.finished("awaiting_review", "awaiting_review")
        self.assertIn("  Status: awaiting_review", lines)
        self.assertEqual(lines[-1], warning(self.held("awaiting_review", operation, candidate)))

    def test_blocked_candidate_is_held_and_exits_zero(self):
        self.write_property_source()

        lines = self.call("import_all_data", *REUSE, "--skip-building", "--skip-gis")

        operation, candidate = self.finished("blocked", "blocked")
        self.assertIn("  Status: blocked", lines)
        self.assertEqual(lines[-1], warning(self.held("blocked", operation, candidate)))

    def test_strict_incomplete_import_fails_with_exit_code_one(self):
        self.write_full_sources()

        raised, lines = self.call_failing(
            CommandError, "Authoritative modern ETL import failed", "import_all_data", *REUSE
        )

        self.assertEqual(raised.returncode, 1)
        self.finished("failed", "blocked")
        self.assertIn("  Status: failed", lines)
        self.assertEqual(lines[-2:], [error("Errors:"), error(f"  - {VALIDATION_GAP}")])
        self.assert_no_held_notice(lines)

    def test_qualified_import_publishes_and_reports_success(self):
        self.publish_baseline("P100")
        self.write_full_sources()

        lines = self.call("import_all_data", *REUSE, "--skip-contract-validation")

        self.finished("published", "published")
        self.assertIn("  Status: completed", lines)
        self.assertEqual(
            lines[-1], success("Authoritative modern ETL import completed successfully.")
        )


class ETLPipelineRunResultTests(HarrisImportResultTestCase):
    def test_blocked_candidate_is_held_and_exits_zero(self):
        self.write_property_source()

        lines = self.call("etl_pipeline", "run", *REUSE, "--scope", "property-only")

        operation, candidate = self.finished("blocked", "blocked")
        self.assertIn("  Status: blocked", lines)
        self.assertEqual(lines[-1], warning(self.held("blocked", operation, candidate)))

    def test_strict_incomplete_run_fails_with_exit_code_one(self):
        self.write_full_sources()

        raised, lines = self.call_failing(
            CommandError, "Pipeline execution failed", "etl_pipeline", "run", *REUSE
        )

        self.assertEqual(raised.returncode, 1)
        self.finished("failed", "blocked")
        self.assertIn("  Status: failed", lines)
        self.assertEqual(lines[-1], error(f"  - {VALIDATION_GAP}"))
        self.assert_no_held_notice(lines)

    def test_best_effort_partial_run_is_held_and_incomplete_and_exits_zero(self):
        self.write_full_sources()

        lines = self.call("etl_pipeline", "run", *REUSE, "--allow-partial")

        operation, candidate = self.finished("partial", "blocked")
        self.assertIn("  Status: partial", lines)
        self.assertIn(error(f"  - {VALIDATION_GAP}"), lines)
        self.assertEqual(
            lines[-1], warning(self.held("blocked (incomplete)", operation, candidate))
        )
        self.assertNotIn(warning("Pipeline completed with partial results."), lines)

    def test_best_effort_partial_preview_reports_partial_results_and_exits_zero(self):
        # Extraction fails without archives, yet the preview still validates the sources it has.
        self.write_property_source()

        lines = self.call(
            "etl_pipeline",
            "run",
            "--skip-download",
            "--scope",
            "property-only",
            "--dry-run",
            "--allow-partial",
        )

        operation = ImportOperation.objects.get()
        self.assertEqual((operation.intent, operation.status), ("preview", "partial"))
        self.assertEqual(lines[-1], warning("Pipeline completed with partial results."))
        self.assert_no_held_notice(lines)

    def test_preview_reports_success(self):
        self.write_property_source()

        lines = self.call("etl_pipeline", "run", *REUSE, "--scope", "property-only", "--dry-run")

        operation = ImportOperation.objects.get()
        self.assertEqual((operation.intent, operation.status), ("preview", "completed"))
        self.assertEqual(lines[-1], success("Pipeline completed successfully!"))


class LoadHcadRealAcctResultTests(HarrisImportResultTestCase):
    @staticmethod
    def summary(status, applied, operation, candidate):
        return (
            f"Status: {status}; applied: {applied}; operation: {operation.pk}; "
            f"candidate: {candidate.pk}. Review in Django admin /admin/data/importcandidate/."
        )

    def test_blocked_candidate_prints_only_the_held_notice_and_exits_zero(self):
        source = self.write_property_source()

        lines = self.call("load_hcad_real_acct", source)

        operation, candidate = self.finished("blocked", "blocked")
        self.assertEqual(lines, [warning(self.held("blocked", operation, candidate))])

    def test_failed_import_prints_the_summary_then_fails_with_exit_code_one(self):
        source = self.write_property_source(state_class="F1")

        raised, lines = self.call_failing(
            CommandError,
            "Property source import failed: Failed processing real_acct.txt: "
            "Refusing to replace property: no loadable rows (invalid=0, skipped=1)",
            "load_hcad_real_acct",
            source,
        )

        self.assertEqual(raised.returncode, 1)
        operation, candidate = self.finished("failed", "blocked")
        self.assertEqual(lines, [self.summary("failed", False, operation, candidate)])

    def test_publication_prints_its_status_line_without_a_review_prompt(self):
        self.publish_baseline("P100", "P200")
        source = self.write_property_source()

        lines = self.call("load_hcad_real_acct", source, "--no-truncate")

        operation, candidate = self.finished("published", "published")
        self.assertEqual(
            lines,
            [
                f"Status: completed; applied: True; operation: {operation.pk}; "
                f"candidate: {candidate.pk}."
            ],
        )


class ImportBuildingDataResultTests(HarrisImportResultTestCase):
    START = success("Starting authoritative building data import...")

    def test_blocked_candidate_is_reported_as_completed_success(self):
        PropertyRecord.objects.create(account_number="P100", is_residential=True)
        self.write_full_sources()

        lines = self.call("import_building_data", "--skip-download", "--no-refresh-readiness")

        self.finished("blocked", "blocked")
        self.assertEqual(
            lines, [self.START, success("Authoritative building import completed (blocked).")]
        )

    def test_strict_incomplete_import_raises_runtime_error_not_command_error(self):
        PropertyRecord.objects.create(account_number="P100", is_residential=True, state_class="A1")
        self.write_full_sources()

        _, lines = self.call_failing(
            RuntimeError,
            f"Authoritative ETL pipeline failed: {VALIDATION_GAP}",
            "import_building_data",
            "--skip-download",
        )

        self.finished("failed", "blocked")
        self.assertEqual(lines, [self.START])

    def test_publication_reports_completed_success(self):
        self.publish_baseline("P100")
        self.write_full_sources()

        lines = self.call("import_building_data", "--skip-download", "--no-refresh-readiness")

        self.finished("published", "published")
        self.assertEqual(
            lines, [self.START, success("Authoritative building import completed (completed).")]
        )


class RunEtlPipelineTaskResultTests(HarrisImportResultTestCase):
    def run_task(self, **kwargs):
        with patch.object(run_etl_pipeline, "update_state") as update_state:
            try:
                return run_etl_pipeline.run(skip_download=True, skip_extract=True, **kwargs)
            finally:
                self.last_state = update_state.call_args.kwargs

    def assert_serialized(self, result, operation, candidate, *, status, wrote_data):
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual(result["status"], status)
        self.assertEqual(result["operation_id"], str(operation.pk))
        self.assertEqual(result["candidate_id"], str(candidate.pk))
        self.assertIs(result["wrote_data"], wrote_data)
        self.assertIs(result["already_applied"], False)
        self.assertEqual(set(result["stages"]), {"source_validation", "load"})
        for stage in result["stages"].values():
            self.assertEqual(set(stage), STAGE_KEYS)

    def test_first_import_is_held_for_review_and_the_task_succeeds(self):
        self.write_full_sources()

        result = self.run_task(validate_contract=False)

        operation, candidate = self.finished("awaiting_review", "awaiting_review")
        self.assert_serialized(
            result, operation, candidate, status="awaiting_review", wrote_data=False
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            self.last_state,
            {
                "state": "SUCCESS",
                "meta": {
                    "step": (
                        "Harris import awaiting_review; published data unchanged. "
                        f"Candidate {candidate.pk}; operation {operation.pk}. "
                        "Review in Django admin /admin/data/importcandidate/."
                    ),
                    **result,
                },
            },
        )

    def test_strict_incomplete_import_fails_the_task(self):
        self.write_full_sources()

        with self.assertRaises(RuntimeError) as raised:
            self.run_task()

        self.assertEqual(
            str(raised.exception), f"Authoritative ETL pipeline failed: {VALIDATION_GAP}"
        )
        self.finished("failed", "blocked")
        self.assertEqual(
            self.last_state,
            {"state": "FAILURE", "meta": {"step": "Pipeline failed", "errors": (VALIDATION_GAP,)}},
        )

    def test_best_effort_partial_import_is_held_incomplete_and_the_task_succeeds(self):
        self.write_full_sources()

        result = self.run_task(strict=False)

        operation, candidate = self.finished("partial", "blocked")
        self.assert_serialized(result, operation, candidate, status="partial", wrote_data=False)
        self.assertEqual(result["errors"], [VALIDATION_GAP])
        self.assertEqual(
            self.last_state,
            {
                "state": "SUCCESS",
                "meta": {
                    "step": (
                        "Harris import blocked (incomplete); published data unchanged. "
                        f"Candidate {candidate.pk}; operation {operation.pk}. "
                        "Review in Django admin /admin/data/importcandidate/."
                    ),
                    **result,
                },
            },
        )

    def test_preview_succeeds_as_pipeline_completed(self):
        self.write_full_sources()

        result = self.run_task(skip_load=True)

        operation = ImportOperation.objects.get()
        self.assertEqual((operation.intent, operation.status), ("preview", "completed"))
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["candidate_id"])
        self.assertIs(result["wrote_data"], False)
        self.assertEqual(
            self.last_state, {"state": "SUCCESS", "meta": {"step": "Pipeline completed"}}
        )

    def test_publication_succeeds_as_pipeline_completed(self):
        self.publish_baseline("P100")
        self.write_full_sources()

        result = self.run_task(validate_contract=False)

        operation, candidate = self.finished("published", "published")
        self.assert_serialized(result, operation, candidate, status="completed", wrote_data=True)
        self.assertEqual(
            self.last_state, {"state": "SUCCESS", "meta": {"step": "Pipeline completed"}}
        )
