"""The Harris import validates and loads the files the HCAD source reading selects (#177).

Both tests call the import's single entry point against real temporary files and observe the
stored evidence and what the load staged in the candidate schema.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
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
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_building_sources,
    _write_property_source,
    extracted,
)
from counties.harris.models import BuildingDetail, ExtraFeature
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId


def _request(extraction: HarrisExtractionMode) -> HarrisImportRequest:
    return HarrisImportRequest(
        plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
        data_year=2026,
        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
        extraction=extraction,
        load=HarrisPrepare(refresh_readiness=False, validate_completeness=False),
    )


def _write_archive(root: str, source_id: HcadSourceId, members: dict[str, str]) -> None:
    archive = (
        Path(root) / "downloads" / DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(source_id).filename
    )
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as contents:
        for name, text in members.items():
            contents.writestr(name, text)


class NestedArchiveTests(TestCase):
    # Keep the candidate's schema to read what the load staged.
    @patch("counties.common.candidate_transitions.drop_candidate_schema")
    def test_room_counts_load_from_a_fixtures_file_the_archive_nests(self, _drop):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_archive(
                root,
                HcadSourceId.REAL_ACCOUNT_OWNER,
                {
                    "2026/real_acct.txt": (
                        "acct\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val\n"
                        "N100\t1 OAK ST\t77001\tA1\t250000\n"
                    )
                },
            )
            _write_archive(
                root,
                HcadSourceId.REAL_BUILDING_LAND,
                {
                    "2026/building_res.txt": (
                        "acct\tbld_num\timprv_type\theat_ar\tbed_rm\tfull_bath\thalf_bath\n"
                        "N100\t1\t1001\t1800\t9\t9\t9\n"
                    ),
                    "2026/fixtures.txt": (
                        "acct\tbld_num\ttype\ttype_dscr\tunits\n"
                        "N100\t1\tRMB\tRoom: Bedroom\t3.00\n"
                        "N100\t1\tRMF\tRoom: Full Bath\t2.00\n"
                        "N100\t1\tRMH\tRoom: Half Bath\t1.00\n"
                    ),
                    "2026/extra_features.txt": "acct\tbld_num\tcd\nN100\t1\tGAR\n",
                },
            )

            result = run_harris_import(_request(HarrisExtractionMode.EXTRACT))

        validation = ImportOperation.objects.get(pk=result.operation_id).evidence["validation"]
        self.assertTrue(validation["valid"], validation["errors"])
        self.assertTrue(result.stages[HarrisImportPhase.LOAD].success, result.errors)
        with switch_search_path(ImportCandidate.objects.get(pk=result.candidate_id).storage_schema):
            rooms = BuildingDetail.objects.values_list("bedrooms", "bathrooms", "half_baths").get(
                account_number="N100", building_number=1
            )
        # bed_rm says 9; only the nested fixtures file says 3 bedrooms and 2.5 baths.
        self.assertEqual(rooms, (3, 2.5, 1))


class StrayExtraFeatureFileTests(TestCase):
    @patch("counties.common.candidate_transitions.drop_candidate_schema")
    def test_a_stray_extra_features_file_is_neither_validated_recorded_nor_loaded(self, _drop):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_property_source(root, account="S100")
            _write_building_sources(root, account="S100")
            (
                extracted(root, HcadSourceId.REAL_BUILDING_LAND) / "extra_features_old.txt"
            ).write_text("acct\tbld_num\tcd\nS100\t1\tOLDX\n", encoding="latin-1")

            result = run_harris_import(_request(HarrisExtractionMode.REUSE_EXTRACTED))

        evidence = ImportOperation.objects.get(pk=result.operation_id).evidence
        validation = evidence["validation"]
        self.assertTrue(validation["valid"], validation["errors"])
        self.assertTrue(result.stages[HarrisImportPhase.LOAD].success, result.errors)
        recorded = [Path(source["path"]).name for source in evidence["sources"]]
        validated = [Path(file["path"]).name for file in validation["files"]]
        self.assertIn("extra_features.txt", recorded)
        self.assertNotIn("extra_features_old.txt", recorded)
        self.assertIn("extra_features.txt", validated)
        self.assertNotIn("extra_features_old.txt", validated)
        self.assertEqual(
            validation["translated"]["extra_features"], {"loaded": 1, "invalid": 0, "skipped": 0}
        )
        with switch_search_path(ImportCandidate.objects.get(pk=result.candidate_id).storage_schema):
            feature_codes = list(ExtraFeature.objects.values_list("feature_code", flat=True))
        self.assertEqual(feature_codes, ["GAR"])
