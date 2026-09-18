"""Contract tests for the deep Harris import boundary."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings

from counties.common.models import ImportCandidate
from counties.harris.etl_pipeline import (
    ExtractedSourceRetention,
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisFailurePolicy,
    HarrisImportPhase,
    HarrisImportRequest,
    HarrisImportStatus,
    HarrisPreview,
    InvalidHarrisImportRequest,
    run_harris_import,
)
from counties.harris.etl_pipeline.candidate import candidate_tables
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord


def _runtime_settings(root: str) -> dict[str, str]:
    base = Path(root)
    return {
        "BASE_DIR": root,
        "HCAD_DOWNLOAD_DIR": str(base / "downloads"),
        "HCAD_EXTRACT_DIR": str(base / "extracted"),
        "HCAD_LOG_DIR": str(base / "logs"),
    }


def _property_plan() -> HarrisImportPlan:
    return HarrisImportPlan.from_legacy_scope("property-only")


def _write_property_source(
    root: str,
    account: str = "P100",
    state_class: str = "A1",
) -> Path:
    source_dir = Path(root) / "extracted" / "Real_acct_owner"
    source_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / "real_acct.txt"
    source.write_text(
        "acct\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val\n"
        f"{account}\t100 MAIN ST\t77001\t{state_class}\t250000\n",
        encoding="latin-1",
    )
    return source


def _write_building_sources(root: str, account: str) -> Path:
    source_dir = Path(root) / "extracted" / "Real_building_land"
    source_dir.mkdir(parents=True, exist_ok=True)
    building_source = source_dir / "building_res.txt"
    building_source.write_text(
        "acct\tbld_num\timprv_type\theat_ar\n" f"{account}\t1\tA1\t1800\n",
        encoding="latin-1",
    )
    (source_dir / "fixtures.txt").write_text(
        "acct\tbld_num\ttype\tunits\n",
        encoding="utf-8",
    )
    (source_dir / "extra_features.txt").write_text(
        "acct\tbld_num\tcd\n" f"{account}\t1\tGAR\n",
        encoding="latin-1",
    )
    return building_source


def _write_extra_feature_detail_sources(root: str, account: str) -> None:
    source_dir = Path(root) / "extracted" / "Real_building_land"
    for filename, feature_code, description in (
        ("extra_features_detail_a.txt", "POOL", "Pool"),
        ("extra_features_detail_b.txt", "GAR", "Garage"),
    ):
        (source_dir / filename).write_text(
            "acct\tbld_num\tcd\tdscr\n" f"{account}\t1\t{feature_code}\t{description}\n",
            encoding="latin-1",
        )


class HarrisImportRequestTests(TestCase):
    def test_rejects_an_impossible_year_before_execution(self):
        with self.assertRaises(InvalidHarrisImportRequest):
            HarrisImportRequest(plan=_property_plan(), data_year=1999)

        with self.assertRaises(InvalidHarrisImportRequest):
            HarrisImportRequest(plan=_property_plan(), data_year="2025")

    def test_preview_type_cannot_express_post_write_options(self):
        request = HarrisImportRequest(plan=_property_plan(), load=HarrisPreview())

        self.assertFalse(hasattr(request.load, "refresh_readiness"))
        self.assertFalse(hasattr(request.load, "validate_completeness"))


class HarrisImportBoundaryTests(TestCase):
    def test_preview_translates_without_writing_or_cleaning(self):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            source = _write_property_source(root)
            request = HarrisImportRequest(
                plan=_property_plan(),
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisPreview(),
            )

            result = run_harris_import(request)

            self.assertIs(result.status, HarrisImportStatus.COMPLETED)
            self.assertFalse(result.wrote_data)
            self.assertEqual(result.stages[HarrisImportPhase.LOAD].metrics["records_loaded"], 1)
            self.assertFalse(PropertyRecord.objects.filter(account_number="P100").exists())
            self.assertTrue(source.exists())

    def test_gis_preview_translates_without_calling_the_persistence_adapter(self):
        import geopandas as gpd
        from shapely.geometry import Point

        with (
            tempfile.TemporaryDirectory() as root,
            override_settings(**_runtime_settings(root)),
            patch("counties.harris.etl_pipeline.gis_loader.load_gis_parcels") as load,
        ):
            source_dir = Path(root) / "extracted" / "Parcels"
            source_dir.mkdir(parents=True)
            shapefile = source_dir / "Parcels.shp"
            gpd.GeoDataFrame(
                {"ACCT": ["P100"], "PARCEL_ID": ["parcel-1"]},
                geometry=[Point(3100000, 13800000)],
                crs="EPSG:2278",
            ).to_file(shapefile)
            contents = shapefile.read_bytes()

            result = run_harris_import(
                HarrisImportRequest(
                    plan=HarrisImportPlan.from_legacy_scope("gis-only"),
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisPreview(),
                )
            )

            self.assertIs(result.status, HarrisImportStatus.COMPLETED)
            self.assertFalse(result.wrote_data)
            self.assertEqual(
                result.stages[HarrisImportPhase.LOAD].metrics["gis_coordinates_updated"], 1
            )
            self.assertEqual(shapefile.read_bytes(), contents)
            load.assert_not_called()

    def test_apply_prepares_then_refreshes_once_without_publishing_unready_data(self):
        with (
            tempfile.TemporaryDirectory() as root,
            override_settings(**_runtime_settings(root)),
            patch("counties.harris.etl_pipeline.readiness.refresh_property_readiness") as refresh,
        ):
            source = _write_property_source(root, account="P200")
            request = HarrisImportRequest(
                plan=_property_plan(),
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisApply(
                    validate_completeness=False,
                    extracted_source_retention=ExtractedSourceRetention.RETAIN,
                ),
            )

            result = run_harris_import(request)

            self.assertIs(result.status, HarrisImportStatus.BLOCKED)
            self.assertFalse(result.wrote_data)
            self.assertFalse(PropertyRecord.objects.filter(account_number="P200").exists())
            self.assertEqual(
                ImportCandidate.objects.get(pk=result.candidate_id).evidence["population"][
                    "properties"
                ],
                1,
            )
            refresh.assert_called_once_with()
            self.assertTrue(source.exists())

    def test_property_and_building_apply_uses_the_rebuilt_property_account_map(self):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_property_source(root, account="P225")
            _write_building_sources(root, account="P225")

            result = run_harris_import(
                HarrisImportRequest(
                    plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisApply(
                        refresh_readiness=False,
                        validate_completeness=False,
                        extracted_source_retention=ExtractedSourceRetention.RETAIN,
                    ),
                )
            )

        self.assertFalse(PropertyRecord.objects.filter(account_number="P225").exists())
        with candidate_tables(ImportCandidate.objects.get(pk=result.candidate_id)):
            property_record = PropertyRecord.objects.get(account_number="P225")
            building = BuildingDetail.objects.get(account_number="P225", building_number=1)
        self.assertIs(result.status, HarrisImportStatus.BLOCKED)
        self.assertEqual(result.stages[HarrisImportPhase.LOAD].metrics["records_loaded"], 3)
        self.assertEqual(building.property_id, property_record.id)

    def test_extra_feature_detail_files_persist_as_one_logical_dataset(self):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_property_source(root, account="P230")
            _write_building_sources(root, account="P230")
            _write_extra_feature_detail_sources(root, account="P230")

            result = run_harris_import(
                HarrisImportRequest(
                    plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisApply(
                        refresh_readiness=False,
                        validate_completeness=False,
                        extracted_source_retention=ExtractedSourceRetention.RETAIN,
                    ),
                )
            )

        self.assertFalse(ExtraFeature.objects.filter(account_number="P230").exists())
        with candidate_tables(ImportCandidate.objects.get(pk=result.candidate_id)):
            features = list(
                ExtraFeature.objects.filter(account_number="P230").order_by("feature_code")
            )
        self.assertIs(result.status, HarrisImportStatus.BLOCKED)
        self.assertEqual(result.stages[HarrisImportPhase.LOAD].metrics["records_loaded"], 4)
        self.assertEqual([feature.feature_code for feature in features], ["GAR", "POOL"])
        self.assertEqual(len({feature.import_batch_id for feature in features}), 1)

    def test_strict_missing_required_extract_returns_failed_result(self):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            result = run_harris_import(
                HarrisImportRequest(
                    plan=_property_plan(),
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisPreview(),
                    failure_policy=HarrisFailurePolicy.STRICT,
                )
            )

            self.assertIs(result.status, HarrisImportStatus.FAILED)
            self.assertTrue(result.errors)

    def test_expected_persistence_failure_is_returned_but_programming_error_raises(self):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_property_source(root, account="P250")
            request = HarrisImportRequest(
                plan=_property_plan(),
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisApply(
                    refresh_readiness=False,
                    validate_completeness=False,
                    extracted_source_retention=ExtractedSourceRetention.RETAIN,
                ),
            )

            with patch(
                "counties.harris.etl_pipeline.persistence.HarrisPersistence.persist",
                side_effect=DatabaseError("database unavailable"),
            ):
                result = run_harris_import(request)

            self.assertIs(result.status, HarrisImportStatus.FAILED)
            self.assertFalse(result.wrote_data)
            self.assertIn("database unavailable", result.errors[0])

            with (
                patch(
                    "counties.harris.etl_pipeline.persistence.HarrisPersistence.persist",
                    side_effect=TypeError("programming defect"),
                ),
                self.assertRaisesRegex(TypeError, "programming defect"),
            ):
                run_harris_import(request)

    def test_empty_property_replace_returns_failed_without_erasing_existing_rows(self):
        PropertyRecord.objects.create(
            account_number="KEEP_EMPTY",
            address="1 KEEP ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_property_source(root, account="SKIPPED", state_class="F1")

            result = run_harris_import(
                HarrisImportRequest(
                    plan=_property_plan(),
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisApply(
                        refresh_readiness=False,
                        validate_completeness=False,
                        extracted_source_retention=ExtractedSourceRetention.RETAIN,
                    ),
                )
            )

        self.assertIs(result.status, HarrisImportStatus.FAILED)
        self.assertIn("no loadable rows", result.errors[0])
        self.assertTrue(PropertyRecord.objects.filter(account_number="KEEP_EMPTY").exists())

    def test_best_effort_missing_sources_cannot_publish_partial_property_work(self):
        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_property_source(root, account="P300")
            result = run_harris_import(
                HarrisImportRequest(
                    plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisApply(
                        refresh_readiness=False,
                        validate_completeness=False,
                        extracted_source_retention=ExtractedSourceRetention.RETAIN,
                    ),
                    failure_policy=HarrisFailurePolicy.BEST_EFFORT,
                )
            )

            self.assertIs(result.status, HarrisImportStatus.FAILED)
            self.assertFalse(result.wrote_data)
            self.assertFalse(PropertyRecord.objects.filter(account_number="P300").exists())
            self.assertTrue(result.errors)

    def test_candidate_preparation_retains_sources_without_early_cleanup(self):
        with (
            tempfile.TemporaryDirectory() as root,
            override_settings(**_runtime_settings(root)),
            patch(
                "counties.harris.etl_pipeline.orchestrator.ExtractManager.cleanup",
                side_effect=PermissionError("locked"),
            ) as cleanup,
        ):
            _write_property_source(root, account="P400")
            result = run_harris_import(
                HarrisImportRequest(
                    plan=_property_plan(),
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisApply(
                        refresh_readiness=False,
                        validate_completeness=False,
                    ),
                )
            )

            self.assertIs(result.status, HarrisImportStatus.BLOCKED)
            self.assertFalse(result.wrote_data)
            cleanup.assert_not_called()
            self.assertTrue(ImportCandidate.objects.get(pk=result.candidate_id).sources)

    def test_reporter_failure_is_observational(self):
        class BrokenReporter:
            def report(self, event):
                raise RuntimeError("observer offline")

        with tempfile.TemporaryDirectory() as root, override_settings(**_runtime_settings(root)):
            _write_property_source(root, account="P500")
            result = run_harris_import(
                HarrisImportRequest(
                    plan=_property_plan(),
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisPreview(),
                ),
                reporter=BrokenReporter(),
            )

            self.assertIs(result.status, HarrisImportStatus.COMPLETED)
            self.assertIn("reporter failed", result.warnings[0])
            self.assertEqual(
                set(result.to_dict()),
                {
                    "status",
                    "started_at",
                    "completed_at",
                    "duration_seconds",
                    "stages",
                    "errors",
                    "warnings",
                },
            )
