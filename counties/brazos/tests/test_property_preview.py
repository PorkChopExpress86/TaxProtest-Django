"""Full source inspection at the authoritative Brazos preview seam."""

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
)
from counties.brazos.tests.test_property_import import _stage_complete_pacs_export
from counties.common.models import ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption


class PropertyPreviewTests(TestCase):
    def preview(self, root, *, annual=False, dry_run=True):
        with self.settings(
            BCAD_DOWNLOAD_DIR=str(root / "downloads"),
            BCAD_EXTRACT_DIR=str(root / "extracted"),
        ):
            return BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(
                PropertyImportRequest(
                    mode=PropertyImportMode.ANNUAL if annual else PropertyImportMode.CAD_RECOVERY,
                    options=RefreshOptions(
                        tax_year=2026,
                        skip_download=True,
                        skip_extract=True,
                        dry_run=dry_run,
                        keep_extracted=True,
                    ),
                )
            )

    def test_partial_preview_measures_every_file_without_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _stage_complete_pacs_export(root, 2026)
            result = self.preview(root)
            self.assertEqual(len(result.cad.metrics), 6)
            operation = ImportOperation.objects.get(pk=result.operation_id)
            self.assertEqual(len(operation.evidence["sources"]), 6)
            self.assertTrue(all(source["sha256"] for source in operation.evidence["sources"]))
            self.assertFalse(PropertyAccount.objects.exists())
            self.assertFalse(BrazosPropertySnapshot.objects.exists())
            self.assertFalse(PropertyJurisdictionExemption.objects.exists())
        self.client.force_login(
            get_user_model().objects.create_superuser("reviewer", password="test")
        )
        response = self.client.get(
            reverse("admin:data_importoperation_change", args=[result.operation_id])
        )
        self.assertContains(response, "Database publication untested")
        self.assertContains(response, "GIS capabilities unavailable")

    def test_malformed_wrong_year_empty_and_conflicting_cad_fail_preview(self):
        for invalid in ("malformed", "wrong year", "empty", "conflict", "numeric"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _stage_complete_pacs_export(root, 2026)
                source = root / "extracted" / "2026" / "APPRAISAL_INFO.TXT"
                original = source.read_text()
                if invalid == "malformed":
                    contents = "bad"
                elif invalid == "wrong year":
                    contents = original.replace("02026", "02025")
                elif invalid == "empty":
                    contents = ""
                elif invalid == "conflict":
                    contents = original + original[:608] + "DIFFERENT" + original[617:]
                else:
                    source = root / "extracted" / "2026" / "APPRAISAL_LAND_DETAIL.TXT"
                    original = source.read_text()
                    contents = original[:140] + "BAD" + original[143:]
                source.write_text(contents)
                with self.assertRaises(CommandError):
                    self.preview(root)
                operation = ImportOperation.objects.latest("started_at")
                self.assertEqual(operation.status, "failed")
                self.assertTrue(operation.evidence["sources"])
                self.assertFalse(PropertyAccount.objects.exists())

    def test_annual_preview_inspects_real_gis_and_rejects_unusable_content(self):
        import geopandas as gpd
        from shapely.geometry import Point

        for prop_id in (10013, "invalid"):
            with self.subTest(prop_id=prop_id), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _stage_complete_pacs_export(root, 2026)
                shape = root / "extracted" / "gis" / "2026" / "parcels.shp"
                shape.parent.mkdir(parents=True)
                gpd.GeoDataFrame(
                    {"PROP_ID": [prop_id]}, geometry=[Point(3556000, 10120000)], crs="EPSG:2277"
                ).to_file(shape)
                if prop_id == "invalid":
                    with self.assertRaises(CommandError):
                        self.preview(root, annual=True)
                else:
                    result = self.preview(root, annual=True)
                    self.assertEqual(result.gis.metrics["usable_coordinate_records"], 1)
                self.assertFalse(BrazosPropertySnapshot.objects.exists())

    def test_changed_retained_preview_input_blocks_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _stage_complete_pacs_export(root, 2026)
            result = self.preview(root)
            operation = ImportOperation.objects.get(pk=result.operation_id)
            Path(operation.evidence["sources"][0]["path"]).write_text("changed")
            with self.assertRaisesRegex(Exception, "changed"):
                self.preview(root, dry_run=False)
            self.assertFalse(BrazosPropertySnapshot.objects.exists())
