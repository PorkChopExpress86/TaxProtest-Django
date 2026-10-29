"""Where each Harris caller finds the GIS parcel layer today.

The canonical layout is the extract root's ``Parcels`` directory. The legacy layout
(extracted files beside the downloaded archive) is still probed, and the callers
disagree about it: validation and ``load_gis_data`` merge it into selection by
priority, while the import's own GIS load refuses a layout without the canonical
directory. These pins record that behaviour as it is, before the catalog owns it.
"""

import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

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
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings

CANONICAL_ONLY = "canonical only"
LEGACY_ONLY = "legacy only"
BOTH = "both present"
LAYOUTS = (CANONICAL_ONLY, LEGACY_ONLY, BOTH)


def _write_layer(path: Path) -> None:
    import geopandas as gpd
    from shapely.geometry import Point

    path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(
        {"ACCT": ["P100"]}, geometry=[Point(3100000, 13800000)], crs="EPSG:2278"
    ).to_file(path)


def _write_layout(root: str, layout: str) -> None:
    # A legacy ParcelsCity layer outranks a canonical parcels layer by name, which is
    # how stale legacy data can win over a fresh extract.
    if layout in (CANONICAL_ONLY, BOTH):
        _write_layer(Path(root) / "extracted" / "Parcels" / "parcels.shp")
    if layout in (LEGACY_ONLY, BOTH):
        _write_layer(Path(root) / "downloads" / "Parcels" / "ParcelsCity.shp")


def _located(root: str, layer: str) -> tuple[str, str]:
    """Return the base root a selected layer came from, and its file name."""
    path = Path(layer).relative_to(root)
    return path.parts[0], path.name


def _gis_request(load) -> HarrisImportRequest:
    return HarrisImportRequest(
        plan=HarrisImportPlan.from_legacy_scope("gis-only"),
        data_year=2026,
        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
        extraction=HarrisExtractionMode.REUSE_EXTRACTED,
        load=load,
    )


class HarrisGisSourceLayoutTests(TestCase):
    def test_preview_validation_merges_the_legacy_layout_by_priority(self):
        expected = {
            CANONICAL_ONLY: ("extracted", "parcels.shp"),
            LEGACY_ONLY: ("downloads", "ParcelsCity.shp"),
            BOTH: ("downloads", "ParcelsCity.shp"),
        }
        for layout in LAYOUTS:
            with (
                self.subTest(layout=layout),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                _write_layout(root, layout)

                result = run_harris_import(_gis_request(HarrisPreview()))

                self.assertTrue(result.success, result.errors)
                validation = ImportOperation.objects.get(pk=result.operation_id).evidence[
                    "validation"
                ]
                (gis,) = [file for file in validation["files"] if file["schema"] == "gis"]
                self.assertEqual(_located(root, gis["path"]), expected[layout])

    def test_apply_requires_the_canonical_layout_and_then_merges_the_legacy_one(self):
        expected = {
            CANONICAL_ONLY: ("extracted", "parcels.shp"),
            LEGACY_ONLY: None,
            BOTH: ("downloads", "ParcelsCity.shp"),
        }
        for layout in LAYOUTS:
            with (
                self.subTest(layout=layout),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
                patch(
                    "counties.harris.etl_pipeline.gis_loader.load_gis_parcels", return_value=1
                ) as load,
            ):
                _write_layout(root, layout)

                result = run_harris_import(_gis_request(HarrisApply(validate_completeness=False)))

                if expected[layout] is None:
                    self.assertFalse(result.success)
                    self.assertIn("GIS extract path not found", " ".join(result.errors))
                    load.assert_not_called()
                else:
                    # A GIS-only apply stops later, short of a full candidate; the layer
                    # handed to the loader is what this pin records.
                    self.assertEqual(_located(root, load.call_args.args[0]), expected[layout])

    def test_load_gis_data_merges_the_legacy_layout_by_priority(self):
        expected = {
            CANONICAL_ONLY: ("extracted", "parcels.shp"),
            LEGACY_ONLY: ("downloads", "ParcelsCity.shp"),
            BOTH: ("downloads", "ParcelsCity.shp"),
        }
        for layout in LAYOUTS:
            with (
                self.subTest(layout=layout),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
                patch(
                    "counties.harris.management.commands.load_gis_data.load_gis_parcels",
                    return_value=1,
                ) as load,
            ):
                _write_layout(root, layout)

                call_command("load_gis_data", "--skip-download", stdout=StringIO())

                self.assertEqual(_located(root, load.call_args.args[0]), expected[layout])
