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
    def test_canonical_fixture_conflict_prevents_validated_success(self):
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_property_source(root)
            _write_building_sources(root, "P100")
            fixtures = Path(root) / "extracted" / "Real_building_land" / "fixtures.txt"
            fixtures.write_text("acct\tbld_num\ttype\tunits\nP100\t01\tRMB\t3\nP100\t1\tRMB\t4\n")
            result = run_harris_import(
                replace(
                    self.request(), plan=HarrisImportPlan.from_legacy_scope("property-and-building")
                )
            )
            self.assertFalse(result.success)
            self.assertIn("conflicting canonical identity", " ".join(result.errors))
            self.assertFalse(PropertyRecord.objects.exists())

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

    def test_gis_conflicts_wrong_year_and_unusable_content_prevent_validated_success(self):
        import geopandas as gpd
        from shapely.geometry import Point

        for coordinates, years in (
            ([Point(3100000, 13800000), Point(3101000, 13800000)], [2026, 2026]),
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
