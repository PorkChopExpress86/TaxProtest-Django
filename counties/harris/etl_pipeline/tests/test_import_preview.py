"""Validated previews inspect retained source content without publishing."""

import hashlib
import tempfile
import zipfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from counties.common.models import ImportOperation
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisPreview,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_building_sources,
    _write_property_source,
)
from counties.harris.models import PropertyRecord


class HarrisValidatedPreviewTests(TestCase):
    def test_conflicting_room_counts_validate_and_are_recorded_invalid(self):
        # HCAD repeats a building's fixture type with different units; only the room
        # types FixturesAggregator reads are canonical identities.
        for fixtures_text, invalid in (
            ("acct\tbld_num\ttype\tunits\nP100\t01\tRMB\t3\nP100\t1\tRMB\t4\n", 2),
            ("acct\tbld_num\ttype\tunits\nP100\t1\tRMB\t3\nP100\t1\tRMB\t3\n", 0),
            ("acct\tbld_num\ttype\tunits\nP100\t1\tWH\t8\nP100\t1\tWH\t10\n", 0),
            # HCAD writes a few rows with no fixture type; nothing reads them.
            ("acct\tbld_num\ttype\tunits\nP100\t1\tRMB\t3\nP100\t2\t    \t0.00\n", 1),
        ):
            with (
                self.subTest(fixtures=fixtures_text),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                _write_property_source(root)
                _write_building_sources(root, "P100")
                fixtures = Path(root) / "extracted" / "Real_building_land" / "fixtures.txt"
                fixtures.write_text(fixtures_text)
                result = run_harris_import(
                    replace(
                        self.request(),
                        plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
                    )
                )
                self.assertTrue(result.success, result.errors)
                validation = ImportOperation.objects.get(pk=result.operation_id).evidence[
                    "validation"
                ]
                (source,) = [file for file in validation["files"] if file["schema"] == "fixtures"]
                self.assertEqual(source["invalid"], invalid)

    def test_features_sharing_code_and_building_validate_as_separate_features(self):
        # HCAD has no feature sequence: two sheds on one lot share acct, cd and bld_num.
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_property_source(root)
            building_source = _write_building_sources(root, "P100")
            (building_source.parent / "extra_features.txt").write_text(
                "acct\tbld_num\tcd\tlength\twidth\n"
                "P100\t0\tRRS1\t10\t11\n"
                "P100\t0\tRRS1\t13\t10\n",
                encoding="latin-1",
            )
            result = run_harris_import(
                replace(
                    self.request(), plan=HarrisImportPlan.from_legacy_scope("property-and-building")
                )
            )
            self.assertTrue(result.success, result.errors)
            validation = ImportOperation.objects.get(pk=result.operation_id).evidence["validation"]
            self.assertEqual(
                validation["translated"]["extra_features"],
                {"loaded": 2, "invalid": 0, "skipped": 0},
            )

    def test_gis_extracted_content_must_match_retained_archive(self):
        import geopandas as gpd
        from shapely.geometry import Point

        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            source = Path(root) / "extracted" / "Parcels" / "parcels.shp"
            source.parent.mkdir(parents=True)
            gpd.GeoDataFrame(
                {"ACCT": ["P100"]}, geometry=[Point(3100000, 13800000)], crs="EPSG:2278"
            ).to_file(source)
            downloads = Path(root) / "downloads"
            downloads.mkdir()
            with zipfile.ZipFile(downloads / "Parcels.zip", "w") as archive:
                for component in source.parent.glob("*"):
                    archive.write(component, component.name)
            gpd.GeoDataFrame(
                {"ACCT": ["CHANGED"]}, geometry=[Point(3101000, 13800000)], crs="EPSG:2278"
            ).to_file(source)
            result = run_harris_import(
                replace(self.request(), plan=HarrisImportPlan.from_legacy_scope("gis-only"))
            )
            self.assertFalse(result.success)
            self.assertIn("extracted content does not match", " ".join(result.errors))
            self.assertFalse(PropertyRecord.objects.exists())

    def test_download_fallback_year_prevents_validation_and_retains_worker_warning(self):
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("real_acct.txt", "acct\tstate_class\nP100\tA1\n")
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.iter_content.return_value = [payload.getvalue()]
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(**_runtime_settings(root)),
            patch("requests.Session.get", return_value=response),
            patch(
                "requests.Session.head",
                side_effect=[MagicMock(status_code=404), MagicMock(status_code=200)],
            ),
        ):
            result = run_harris_import(
                replace(
                    self.request(),
                    acquisition=HarrisAcquisitionMode.FETCH,
                    extraction=HarrisExtractionMode.EXTRACT,
                )
            )
            self.assertFalse(result.success)
            user = get_user_model().objects.create_superuser("viewer", password="test")
            self.client.force_login(user)
            detail = self.client.get(
                reverse("admin:data_importoperation_change", args=[result.operation_id])
            )
            for text in (
                "source year 2025",
                "Falling back",
                "2025/Real_acct_owner.zip",
                "Database publication untested",
            ):
                self.assertContains(detail, text)

    def test_split_gis_parcels_validate_and_are_flagged_in_the_evidence(self):
        import geopandas as gpd
        from shapely.geometry import Point

        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            source = Path(root) / "extracted" / "Parcels" / "parcels.shp"
            source.parent.mkdir(parents=True)
            gpd.GeoDataFrame(
                {"ACCT": ["P200", "P100", "P100"]},
                geometry=[
                    Point(3102000, 13800000),
                    Point(3100000, 13800000),
                    Point(3101000, 13800000),
                ],
                crs="EPSG:2278",
            ).to_file(source)
            result = run_harris_import(
                replace(self.request(), plan=HarrisImportPlan.from_legacy_scope("gis-only"))
            )
            self.assertTrue(result.success, result.errors)
            validation = ImportOperation.objects.get(pk=result.operation_id).evidence["validation"]
            (gis,) = [file for file in validation["files"] if file["schema"] == "gis"]
            self.assertEqual(gis["merged_accounts"], ["P100"])
            self.assertEqual(
                validation["translated"]["gis"], {"loaded": 2, "invalid": 0, "skipped": 1}
            )

    def test_fetched_parcels_record_the_year_named_by_the_download_url(self):
        # HCAD's parcel layer has no tax-year column; only the yearly archive name dates it.
        import geopandas as gpd
        from shapely.geometry import Point

        yearly = "https://download.hcad.org/data/GIS/Parcels_2026_Oct.zip"
        current = "https://download.hcad.org/data/GIS/Parcels.zip"
        for head_statuses, source_url, source_year in (
            ([200], yearly, 2026),
            ([404, 200], current, None),
        ):
            with (
                self.subTest(source_url=source_url),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                layer = Path(root) / "layer" / "parcels.shp"
                layer.parent.mkdir()
                gpd.GeoDataFrame(
                    {"ACCT": ["P100"]}, geometry=[Point(3100000, 13800000)], crs="EPSG:2278"
                ).to_file(layer)
                payload = BytesIO()
                with zipfile.ZipFile(payload, "w") as archive:
                    for component in layer.parent.glob("*"):
                        archive.write(component, f"Parcels/{component.name}")
                response = MagicMock()
                response.__enter__.return_value = response
                response.headers = {}
                response.iter_content.return_value = [payload.getvalue()]
                with (
                    patch("requests.Session.get", return_value=response) as get,
                    patch(
                        "requests.Session.head",
                        side_effect=[MagicMock(status_code=code) for code in head_statuses],
                    ),
                ):
                    result = run_harris_import(
                        replace(
                            self.request(),
                            plan=HarrisImportPlan.from_legacy_scope("gis-only"),
                            acquisition=HarrisAcquisitionMode.FETCH,
                            extraction=HarrisExtractionMode.EXTRACT,
                        )
                    )
                self.assertTrue(result.success, result.errors)
                self.assertEqual(get.call_args.args[0], source_url)
                operation = ImportOperation.objects.get(pk=result.operation_id)
                parcel_sources = [
                    source
                    for source in operation.evidence["sources"]
                    if source["source_id"] == "gis-parcels"
                ]
                self.assertTrue(parcel_sources)
                self.assertEqual(
                    {source["source_year"] for source in parcel_sources}, {source_year}
                )
                (gis,) = [
                    file
                    for file in operation.evidence["validation"]["files"]
                    if file["schema"] == "gis"
                ]
                if source_year is None:
                    self.assertIs(gis["source_year_unconfirmed"], True)
                else:
                    self.assertNotIn("source_year_unconfirmed", gis)

    def test_gis_wrong_year_and_unusable_content_prevent_validated_success(self):
        import geopandas as gpd
        from shapely.geometry import Point

        for coordinates, years in (
            ([Point(3100000, 13800000)], [2025]),
            ([None], [2026]),
        ):
            with (
                self.subTest(years=years),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                source = Path(root) / "extracted" / "Parcels" / "parcels.shp"
                source.parent.mkdir(parents=True)
                gpd.GeoDataFrame(
                    {"ACCT": ["P100"] * len(coordinates), "TaxYear": years},
                    geometry=coordinates,
                    crs="EPSG:2278",
                ).to_file(source)
                result = run_harris_import(
                    replace(self.request(), plan=HarrisImportPlan.from_legacy_scope("gis-only"))
                )
                self.assertFalse(result.success)
                self.assertFalse(PropertyRecord.objects.exists())

    def test_content_defects_prevent_validated_success_and_preserve_published_facts(self):
        PropertyRecord.objects.create(
            account_number="LIVE",
            address="Published facts",
            is_residential=True,
            is_data_ready=True,
        )
        for text in (
            "state_class\nA1\n",
            "acct\tstate_class\tyr\nP100\tA1\t2025\n",
            "acct\tstate_class\ttot_appr_val\nP100\tA1\t200000\nP100\tA1\t300000\n",
            "acct\tstate_class\nP100\n",
            "acct\tstate_class\ttot_appr_val\nP100\tA1\tnot a number\n",
            "acct\tstate_class\nACCOUNT-IDENTITY-TOO-LONG\tA1\n",
        ):
            with (
                self.subTest(text=text),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                _write_property_source(root).write_text(text)
                result = run_harris_import(self.request())
                self.assertFalse(result.success)
                self.assertFalse(result.wrote_data)
                self.assertEqual(PropertyRecord.objects.get().address, "Published facts")

    def request(self):
        return HarrisImportRequest(
            plan=HarrisImportPlan.from_legacy_scope("property-only"),
            data_year=2026,
            acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
            extraction=HarrisExtractionMode.REUSE_EXTRACTED,
            load=HarrisPreview(),
        )

    def test_site_features_numbered_zero_validate_but_other_feature_numbers_do_not(self):
        # HCAD numbers features that belong to no building (paving, sheds) as bld_num 0.
        for feature_number, valid in (("0", True), ("2", True), ("-1", False), ("1.5", False)):
            with (
                self.subTest(feature_number=feature_number),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                _write_property_source(root)
                building_source = _write_building_sources(root, "P100")
                (building_source.parent / "extra_features.txt").write_text(
                    f"acct\tbld_num\tcd\nP100\t{feature_number}\tCPA1\n", encoding="latin-1"
                )
                result = run_harris_import(
                    replace(
                        self.request(),
                        plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
                    )
                )
                self.assertEqual(result.success, valid)
                if valid:
                    operation = ImportOperation.objects.get(pk=result.operation_id)
                    self.assertEqual(
                        operation.evidence["validation"]["translated"]["extra_features"]["loaded"],
                        1,
                    )

    def test_features_hcad_has_not_valued_yet_validate_but_other_text_values_do_not(self):
        # HCAD writes "Pending" for asd_val until it has appraised the feature.
        for value, valid in (("Pending", True), ("not a number", False)):
            with (
                self.subTest(value=value),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                _write_property_source(root)
                building_source = _write_building_sources(root, "P100")
                (building_source.parent / "extra_features.txt").write_text(
                    f"acct\tbld_num\tcd\tasd_val\nP100\t0\tRRS1\t{value}\n", encoding="latin-1"
                )
                result = run_harris_import(
                    replace(
                        self.request(),
                        plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
                    )
                )
                self.assertEqual(result.success, valid)

    def test_rows_that_do_not_match_the_header_are_excluded_and_recorded(self):
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_property_source(root).write_text(
                "acct\tsite_addr_1\tstate_class\ttot_appr_val\n"
                "P100\t100 MAIN ST\tA1\t250000\n"
                "P200\t\t200 MAIN ST\tA1\t250000\n",
                encoding="latin-1",
            )
            result = run_harris_import(self.request())
            self.assertTrue(result.success)
            validation = ImportOperation.objects.get(pk=result.operation_id).evidence["validation"]
            (source,) = validation["files"]
            self.assertEqual(source["invalid"], 1)
            self.assertEqual(validation["translated"]["real_acct"]["loaded"], 1)

    def test_preview_binds_exact_content_and_building_translation_to_selected_new_accounts(self):
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            source = _write_property_source(root)
            _write_building_sources(root, "P100")
            result = run_harris_import(
                replace(
                    self.request(), plan=HarrisImportPlan.from_legacy_scope("property-and-building")
                )
            )
            self.assertTrue(result.success)
            self.assertFalse(PropertyRecord.objects.exists())
            user = get_user_model().objects.create_superuser("viewer", password="test")
            self.client.force_login(user)
            detail = self.client.get(
                reverse("admin:data_importoperation_change", args=[result.operation_id])
            )
            for text in (
                "Database publication untested",
                hashlib.sha256(source.read_bytes()).hexdigest(),
                "building_res.txt",
                "records_invalid",
                "0",
            ):
                self.assertContains(detail, text)
            operation = ImportOperation.objects.get(pk=result.operation_id)
            self.assertEqual(
                operation.evidence["validation"]["translated"]["building_res"]["loaded"], 1
            )

    def test_changed_retained_preview_content_cannot_be_reused_for_apply(self):
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_property_source(root)
            preview = run_harris_import(self.request())
            operation = ImportOperation.objects.get(pk=preview.operation_id)
            source = next(
                Path(item["path"])
                for item in operation.evidence["sources"]
                if item["path"].endswith("real_acct.txt")
            )
            source.write_text("acct\tstate_class\nALTERED\tA1\n")
            with self.assertRaisesRegex(RuntimeError, "Retained source files changed"):
                run_harris_import(replace(self.request(), load=HarrisApply()))
            self.assertFalse(PropertyRecord.objects.exists())
