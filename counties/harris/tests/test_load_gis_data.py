import tempfile
import warnings
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from counties.common.models import ImportOperation
from counties.harris.management.commands.load_gis_data import find_preferred_shapefile
from counties.harris.models import PropertyRecord
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId


class FindPreferredShapefileTests(SimpleTestCase):
    def test_prefers_nested_parcelscity_shapefile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Parcels.shp").touch()
            nested = root / "Parcels" / "Gis" / "pdata" / "ParcelsCity"
            nested.mkdir(parents=True)
            preferred = nested / "ParcelsCity.shp"
            preferred.touch()

            self.assertEqual(find_preferred_shapefile(str(root)), str(preferred))

    def test_falls_back_to_available_shapefile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            only_file = root / "Parcels.shp"
            only_file.touch()

            self.assertEqual(find_preferred_shapefile(str(root)), str(only_file))

    def test_returns_none_when_no_shapefile_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(find_preferred_shapefile(tmpdir))


class CentroidCrsTests(TestCase):
    """Parcel centroids must be computed in the shapefile's projected CRS.

    HCAD's Parcels shapefile is projected (US survey feet); taking a centroid
    after reprojecting to EPSG:4326 runs a planar calculation on degrees,
    which geopandas warns about. Mirrors the equivalent Brazos test.
    """

    def _write_polygon_shapefile(self, path: Path):
        import geopandas as gpd
        from shapely.geometry import Polygon

        # An elongated parcel in EPSG:2278 (NAD83 / Texas South Central, ft),
        # the CRS HCAD publishes Parcels.shp in.
        self.parcel = Polygon(
            [(3100000, 13800000), (3101500, 13800000), (3101500, 13800400), (3100000, 13800400)]
        )
        gpd.GeoDataFrame(
            {"ACCT": ["GISCRS001"], "PARCEL_ID": ["P1"]},
            geometry=[self.parcel],
            crs="EPSG:2278",
        ).to_file(path)

    def test_centroid_is_computed_before_reprojection(self):
        import geopandas as gpd

        from counties.harris.etl_pipeline.gis_loader import load_gis_parcels

        PropertyRecord.objects.create(
            address="1 Centroid St",
            city="Houston",
            zipcode="77002",
            account_number="GISCRS001",
            state_class="A1",
            is_residential=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            shp = Path(tmp) / "parcels.shp"
            self._write_polygon_shapefile(shp)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                load_gis_parcels(str(shp), refresh_readiness=False)

            expected = gpd.GeoSeries([self.parcel], crs="EPSG:2278").centroid.to_crs(epsg=4326)

        self.assertEqual(
            [w for w in caught if "geographic CRS" in str(w.message)],
            [],
            "taking a centroid in EPSG:4326 warns; compute it in the projected CRS first",
        )

        record = PropertyRecord.objects.get(account_number="GISCRS001")
        self.assertAlmostEqual(float(record.latitude), float(expected.y.iloc[0]), places=6)
        self.assertAlmostEqual(float(record.longitude), float(expected.x.iloc[0]), places=6)


class LoadGisDataProvenanceTests(TestCase):
    """The command records the parcel URL it downloaded and the year that URL names."""

    def _run(self, downloaded_url: str) -> ImportOperation:
        import geopandas as gpd
        from shapely.geometry import Point

        with tempfile.TemporaryDirectory() as tmp:
            dirname = DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(HcadSourceId.GIS_PARCELS)
            layer = Path(tmp) / dirname / "parcels.shp"
            layer.parent.mkdir()
            gpd.GeoDataFrame(
                {"ACCT": ["GIS001"]}, geometry=[Point(3100000, 13800000)], crs="EPSG:2278"
            ).to_file(layer)
            downloads = MagicMock()
            downloads.download_file.return_value = MagicMock(
                success=True, local_path=Path(tmp) / "Parcels.zip", source_url=downloaded_url
            )
            extracts = MagicMock()
            extracts.extract_archive.return_value = MagicMock(success=True)
            extracts.get_extract_path.return_value = layer.parent
            module = "counties.harris.management.commands.load_gis_data"
            with (
                patch(f"{module}.DownloadManager", return_value=downloads),
                patch(f"{module}.ExtractManager", return_value=extracts),
                patch(f"{module}.load_gis_parcels", return_value=0),
            ):
                call_command("load_gis_data")
        return ImportOperation.objects.get(intent="gis_coordinates")

    def test_a_yearly_download_records_its_year(self) -> None:
        year = datetime.now().year
        url = f"https://download.hcad.org/data/GIS/Parcels_{year}_Oct.zip"
        operation = self._run(url)

        self.assertEqual({s["source_url"] for s in operation.evidence["sources"]}, {url})
        self.assertEqual({s["source_year"] for s in operation.evidence["sources"]}, {year})
        self.assertNotIn("source_year_unconfirmed", operation.evidence)

    def test_the_current_file_fallback_is_flagged_unconfirmed(self) -> None:
        url = "https://download.hcad.org/data/GIS/Parcels.zip"
        operation = self._run(url)

        self.assertEqual({s["source_url"] for s in operation.evidence["sources"]}, {url})
        self.assertEqual({s["source_year"] for s in operation.evidence["sources"]}, {None})
        self.assertIs(operation.evidence["source_year_unconfirmed"], True)
