"""One HCAD source reading serves both source validation and the staged load (#178).

The test calls the import's single entry point against real temporary files and observes how
often the fixtures member is read into room counts and what the load staged from it.
"""

from __future__ import annotations

import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from counties.common.candidate_staging import switch_search_path
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportPhase,
    HarrisImportRequest,
    HarrisPrepare,
    run_harris_import,
)
from counties.harris.etl_pipeline.fixtures_aggregator import FixturesAggregator
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_building_sources,
    _write_property_source,
    extracted,
)
from counties.harris.models import BuildingDetail
from counties.harris.source_catalog import HcadSourceId


class RoomCountsReadOnceTests(TestCase):
    # Keep the candidate's schema to read what the load staged.
    @patch("counties.common.candidate_transitions.drop_candidate_schema")
    def test_a_prepared_import_reads_the_fixtures_member_once_for_validation_and_load(self, _drop):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_property_source(root, account="F100")
            _write_building_sources(root, account="F100")
            fixtures = extracted(root, HcadSourceId.REAL_BUILDING_LAND) / "fixtures.txt"
            fixtures.write_text(
                "acct\tbld_num\ttype\ttype_dscr\tunits\n"
                "F100\t1\tRMB\tRoom: Bedroom\t4.00\n"
                "F100\t1\tRMF\tRoom: Full Bath\t2.00\n",
                encoding="utf-8",
            )
            real_read = FixturesAggregator.load_fixtures_file

            with patch.object(
                FixturesAggregator, "load_fixtures_file", autospec=True, side_effect=real_read
            ) as read:
                result = run_harris_import(
                    HarrisImportRequest(
                        plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
                        data_year=2026,
                        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                        extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                        load=HarrisPrepare(refresh_readiness=False, validate_completeness=False),
                    )
                )

        validation = ImportOperation.objects.get(pk=result.operation_id).evidence["validation"]
        self.assertTrue(validation["valid"], validation["errors"])
        self.assertTrue(result.stages[HarrisImportPhase.LOAD].success, result.errors)
        # The import reads a working copy of the extract; the one read is its fixtures member.
        self.assertEqual(
            [call.args[1].parts[-2:] for call in read.call_args_list], [fixtures.parts[-2:]]
        )
        with switch_search_path(ImportCandidate.objects.get(pk=result.candidate_id).storage_schema):
            rooms = BuildingDetail.objects.values_list("bedrooms", "bathrooms").get(
                account_number="F100", building_number=1
            )
        # The load staged the room counts that single read produced.
        self.assertEqual(rooms, (4, 2.0))
