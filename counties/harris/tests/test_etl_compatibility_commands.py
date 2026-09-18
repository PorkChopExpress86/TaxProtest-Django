"""Compatibility command contracts for the authoritative Harris ETL path."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase

from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisFailurePolicy,
    HarrisPreview,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan


class ImportBuildingDataCommandTests(SimpleTestCase):
    @patch("counties.harris.management.commands.import_building_data._run_authoritative_pipeline")
    def test_sync_command_builds_one_plan_and_preserves_skip_flags(self, mocked_run):
        mocked_run.return_value = {"status": "completed"}

        call_command(
            "import_building_data",
            skip_download=True,
            with_gis=True,
            no_refresh_readiness=True,
        )

        mocked_run.assert_called_once()
        _, kwargs = mocked_run.call_args
        self.assertEqual(
            kwargs["plan"],
            HarrisImportPlan.from_stage_flags(
                include_property=False,
                include_building=True,
                include_gis=True,
            ),
        )
        self.assertTrue(kwargs["skip_download"])
        self.assertTrue(kwargs["skip_extract"])
        self.assertFalse(kwargs["refresh_readiness"])
        self.assertFalse(kwargs["validate_contract"])

    @patch("counties.harris.management.commands.import_building_data.run_etl_pipeline.delay")
    def test_async_command_serializes_the_plan_as_a_compatibility_scope(self, mocked_delay):
        mocked_delay.return_value = MagicMock(id="task-123")

        call_command("import_building_data", **{"async": True})

        mocked_delay.assert_called_once_with(
            skip_download=False,
            skip_extract=False,
            scope="building-only",
            strict=True,
            refresh_readiness=True,
            validate_contract=True,
        )


class ETLPipelineCommandTests(SimpleTestCase):
    @patch("counties.harris.management.commands.etl_pipeline.run_harris_import")
    def test_run_translates_legacy_flags_at_the_cli_boundary(self, mocked_import):
        mocked_import.return_value = SimpleNamespace(
            success=True,
            status=SimpleNamespace(value="completed"),
            duration=0.1,
            stages={},
            errors=(),
        )

        call_command(
            "etl_pipeline",
            "run",
            "--skip-download",
            "--skip-extract",
            "--dry-run",
            "--allow-partial",
            "--scope",
            "gis-only",
            "--year",
            "2025",
        )

        request = mocked_import.call_args.args[0]
        self.assertEqual(request.plan, HarrisImportPlan.from_legacy_scope("gis-only"))
        self.assertEqual(request.data_year, 2025)
        self.assertIs(request.acquisition, HarrisAcquisitionMode.REUSE_DOWNLOADED)
        self.assertIs(request.extraction, HarrisExtractionMode.REUSE_EXTRACTED)
        self.assertIsInstance(request.load, HarrisPreview)
        self.assertIs(request.failure_policy, HarrisFailurePolicy.BEST_EFFORT)


class LoadHcadRealAcctCommandTests(SimpleTestCase):
    @patch("counties.harris.management.commands.load_hcad_real_acct.run_harris_import")
    def test_command_delegates_property_file_to_authoritative_import(self, mocked_import):
        mocked_import.return_value = SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            wrote_data=True,
            operation_id="operation-123",
            candidate_id="candidate-123",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "real_acct.txt"
            filepath.write_text("acct\tstate_class\n", encoding="utf-8")
            call_command(
                "load_hcad_real_acct",
                str(filepath),
                chunk=125,
                limit=2,
                no_truncate=True,
            )

        mocked_import.assert_called_once()
        request = mocked_import.call_args.args[0]
        self.assertEqual(request.plan, HarrisImportPlan.from_legacy_scope("property-only"))
        self.assertIs(request.acquisition, HarrisAcquisitionMode.REUSE_DOWNLOADED)
        self.assertIs(request.extraction, HarrisExtractionMode.REUSE_EXTRACTED)
        self.assertIsInstance(request.load, HarrisApply)
        self.assertTrue(request.load.refresh_readiness)
        self.assertFalse(request.load.validate_completeness)
        self.assertEqual(request.property_file.path, filepath)
        self.assertEqual(request.property_file.batch_size, 125)
        self.assertEqual(request.property_file.limit, 2)
        self.assertTrue(request.property_file.append)
        self.assertEqual(request.origin, "command")
