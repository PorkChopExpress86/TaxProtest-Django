"""Compatibility command contracts for the authoritative Harris ETL path."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase

from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.translated_loader import TranslatedLoadResult


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


class LoadHcadRealAcctCommandTests(SimpleTestCase):
    @patch("counties.harris.management.commands.load_hcad_real_acct.refresh_property_readiness")
    @patch("counties.harris.management.commands.load_hcad_real_acct.load_property_file")
    @patch("counties.harris.management.commands.load_hcad_real_acct.ETLConfig.from_env")
    def test_command_uses_the_translated_row_loader(
        self,
        mocked_config,
        mocked_load,
        mocked_readiness,
    ):
        mocked_load.return_value = TranslatedLoadResult(
            records_loaded=2,
            records_invalid=0,
            records_skipped=1,
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

        mocked_config.assert_called_once_with()
        mocked_load.assert_called_once_with(
            mocked_config.return_value,
            filepath,
            batch_size=125,
            limit=2,
            truncate=False,
        )
        mocked_readiness.assert_called_once_with()
