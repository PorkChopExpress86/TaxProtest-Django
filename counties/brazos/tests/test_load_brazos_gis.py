"""Tests for the load_brazos_gis management command.

ScrapeGisArchiveTests uses a fixture mirroring the real
brazoscad.org/tax-information/gis/ page, which lists several .zip links that
are NOT parcel-boundary data for a given year (a "Monthly Shapefiles"
variant, map-book downloads) alongside the real "<year> Certified
Shapefiles Download" links -- the regression this test guards is picking the
wrong link, not just finding *a* .zip.

LoadGisDataTests builds a real, tiny shapefile via geopandas/shapely (not a
mock) so the actual CRS reprojection and centroid-computation code path runs
end-to-end, matching the project's convention elsewhere of testing against
real file formats rather than mocked internals.
"""

from __future__ import annotations

import tempfile
import warnings
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from counties.brazos.management.commands.load_brazos_gis import Command
from counties.brazos.models import PropertyAccount

# Real page structure (see docs/research/brazos-gis-parcel-shapefile.md):
# certified-year links, a non-year-labeled monthly variant, and unrelated
# map-book downloads all coexist as plain .zip links on the same page.
GIS_PORTAL_FIXTURE_HTML = """
<html><body>
<a href="/wp-content/uploads/2026/05/BrazosCADParcels_20260422.zip">2025 Certified Shapefiles Download</a>
<a href="/wp-content/uploads/2024/08/Public_Parcel_Boundary_certified.zip">2024 Certified Shapefiles Download</a>
<a href="/wp-content/uploads/2024/03/PUBLIC_PARCEL_BOUNDARY.zip">Brazos County Monthly Shapefiles Download</a>
<a href="/wp-content/uploads/2023/08/Map_Books-08-02-19.zip">Brazos County Map Book Download</a>
</body></html>
"""


def _mock_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


class ScrapeGisArchiveTests(TestCase):
    def test_picks_latest_certified_year_not_monthly_or_map_book(self):
        with patch(
            "counties.brazos.management.commands.load_brazos_gis.requests.get",
            return_value=_mock_response(GIS_PORTAL_FIXTURE_HTML),
        ):
            url, year = Command()._scrape_archive("https://brazoscad.org/tax-information/gis/")

        self.assertTrue(url.endswith("BrazosCADParcels_20260422.zip"))
        self.assertEqual(year, 2025)

    def test_raises_when_no_certified_link_found(self):
        html = """
        <html><body>
        <a href="/files/Map_Books.zip">Brazos County Map Book Download</a>
        </body></html>
        """
        with patch(
            "counties.brazos.management.commands.load_brazos_gis.requests.get",
            return_value=_mock_response(html),
        ):
            with self.assertRaises(CommandError):
                Command()._scrape_archive("https://brazoscad.org/tax-information/gis/")


def write_fixture_shapefile(path: Path) -> None:
    """Build a real, tiny EPSG:2277 shapefile so the reprojection code runs."""
    import geopandas as gpd
    from shapely.geometry import Point

    # EPSG:2277 (NAD83 / Texas Central, US feet) coordinates that land
    # near real Bryan, TX after reprojection -- same CRS as the real
    # export, so this exercises the actual reprojection code path.
    gdf = gpd.GeoDataFrame(
        {
            "PROP_ID": [10013, 10055],
            "situs_num": ["5000", ""],
            "situs_stre": ["SILVER HILL", ""],
            "situs_st_1": ["RD", ""],
            "situs_st_2": ["", ""],
            "situs_unit": ["", ""],
            "addr_city": ["COLLEGE STATION", "BRYAN"],
            "addr_state": ["TX", "TX"],
            "addr_zip": ["77845-8087", "77801"],
            "market": [242613.0, 100000.0],
            "Land_Val": [200000.0, 50000.0],
            "Imprv_Val": [42613.0, 50000.0],
            "state_cd": ["E1", "A1"],
            "living_are": [None, 1800.0],
            "class_cd": ["RV3", "RF4"],
            "yr_built": [1980, None],
            "yr_blt": [1980, 1998],
        },
        geometry=[Point(3556000, 10120000), Point(3560000, 10125000)],
        crs="EPSG:2277",
    )
    gdf.to_file(path)


class LoadGisDataTests(TestCase):
    def _write_fixture_shapefile(self, path: Path) -> None:
        write_fixture_shapefile(path)

    def test_updates_matching_property_accounts_from_real_shapefile(self):
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)
        PropertyAccount.objects.create(prop_id="000000010055", tax_year=2025)

        with self.settings():
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                shp_path = Path(tmp) / "parcels.shp"
                self._write_fixture_shapefile(shp_path)

                results = Command()._load(shp_path, 2025, dry_run=False)

        self.assertEqual(results["matched"], 2)
        self.assertEqual(results["unmatched"], 0)

        row = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertEqual(row.situs_address, "5000 SILVER HILL RD")
        self.assertEqual(row.situs_state, "TX")
        self.assertEqual(row.state_class, "E1")
        self.assertEqual(row.year_built, 1980)
        self.assertEqual(row.class_code, "RV3")
        self.assertIsNotNone(row.latitude)
        self.assertIsNotNone(row.longitude)
        # Real Brazos County coordinates: roughly 30.5-30.8N, -96.2 to -96.5W.
        self.assertTrue(30 < row.latitude < 31)
        self.assertTrue(-97 < row.longitude < -96)

        row2 = PropertyAccount.objects.get(prop_id="000000010055", tax_year=2025)
        self.assertEqual(row2.situs_address, "")
        self.assertEqual(row2.year_built, 1998, "should fall back to yr_blt when yr_built is blank")

    def test_does_not_write_unreliable_year_value_fields(self):
        """Regression guard: the shapefile's market/Land_Val/Imprv_Val fields
        are current/preliminary, not certified-year (confirmed against a real
        property on BCAD's own live search -- our stored value matched the
        *next* tax year, not the target one). load_brazos_gis must never set
        total_value/land_value/improvement_value/assessed_value -- assessed_value
        is rolled up elsewhere (load_brazos_cad, from APPRAISAL_ENTITY_INFO.TXT),
        and the other three have no verified source at all."""
        import tempfile

        PropertyAccount.objects.create(
            prop_id="000000010013", tax_year=2025, assessed_value=Decimal("242613")
        )

        with tempfile.TemporaryDirectory() as tmp:
            shp_path = Path(tmp) / "parcels.shp"
            self._write_fixture_shapefile(shp_path)
            Command()._load(shp_path, 2025, dry_run=False)

        row = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertIsNone(row.total_value)
        self.assertIsNone(row.land_value)
        self.assertIsNone(row.improvement_value)
        # assessed_value must survive untouched -- load_brazos_gis has no
        # business overwriting a value rolled up from a different, more
        # reliable source.
        self.assertEqual(row.assessed_value, Decimal("242613"))

    def test_does_not_touch_situs_city_or_zip(self):
        """Regression guard: addr_city/addr_state/addr_zip are MAILING fields
        (confirmed via docs/research/brazos-gis-parcel-shapefile.md) and must
        never be written into situs_city/situs_zip -- that would reintroduce
        the exact mailing/situs conflation this command exists to fix."""
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            shp_path = Path(tmp) / "parcels.shp"
            self._write_fixture_shapefile(shp_path)
            Command()._load(shp_path, 2025, dry_run=False)

        row = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertEqual(row.situs_city, "")
        self.assertEqual(row.situs_zip, "")

    def test_literal_null_string_is_stripped_not_concatenated(self):
        """Regression guard: the real export encodes a missing string field as
        the literal text "NULL" (e.g. situs_stre is 'NULL' on 71,239/77,433
        real rows), not an actual null/NaN -- a naive join would produce
        addresses like "5160 NULL TWIN HILL DR" for the vast majority of
        Brazos properties."""
        import geopandas as gpd
        from shapely.geometry import Point

        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)
        gdf = gpd.GeoDataFrame(
            {
                "PROP_ID": [10013],
                "situs_num": ["5160"],
                "situs_stre": ["NULL"],
                "situs_st_1": ["TWIN HILL (PVT)"],
                "situs_st_2": ["DR"],
                "situs_unit": ["NULL"],
                "class_cd": ["NULL"],
            },
            geometry=[Point(3556000, 10120000)],
            crs="EPSG:2277",
        )
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            shp_path = Path(tmp) / "parcels.shp"
            gdf.to_file(shp_path)
            Command()._load(shp_path, 2025, dry_run=False)

        row = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertEqual(row.situs_address, "5160 TWIN HILL (PVT) DR")
        self.assertNotIn("NULL", row.situs_address)
        self.assertEqual(row.class_code, "")

    def test_prop_id_without_int_value_is_skipped(self):
        import geopandas as gpd
        from shapely.geometry import Point

        gdf = gpd.GeoDataFrame(
            {"PROP_ID": [None]},
            geometry=[Point(3556000, 10120000)],
            crs="EPSG:2277",
        )
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            shp_path = Path(tmp) / "parcels.shp"
            gdf.to_file(shp_path)

            results = Command()._load(shp_path, 2025, dry_run=False)

        self.assertEqual(results["matched"], 0)


class CentroidCrsTests(TestCase):
    """Centroids belong in the projected CRS, not in lat/long degrees.

    The other fixtures use Point geometries, whose centroid is the point
    itself -- they cannot tell the two orderings apart. These use real
    polygons.
    """

    def _write_polygon_shapefile(self, path: Path) -> None:
        import geopandas as gpd
        from shapely.geometry import Polygon

        # An elongated parcel in EPSG:2277 (NAD83 / Texas Central, US feet).
        self.parcel = Polygon(
            [(3556000, 10120000), (3557500, 10120000), (3557500, 10120400), (3556000, 10120400)]
        )
        gpd.GeoDataFrame({"PROP_ID": [10013]}, geometry=[self.parcel], crs="EPSG:2277").to_file(
            path
        )

    def _expected_lat_lon(self):
        import geopandas as gpd

        centroid = gpd.GeoSeries([self.parcel], crs="EPSG:2277").centroid.to_crs(epsg=4326)
        return float(centroid.y.iloc[0]), float(centroid.x.iloc[0])

    def test_centroid_is_computed_before_reprojection(self):
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)

        with tempfile.TemporaryDirectory() as tmp:
            shp = Path(tmp) / "parcels.shp"
            self._write_polygon_shapefile(shp)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                Command()._load(shp, 2025, dry_run=False)

        geographic_crs_warnings = [w for w in caught if "geographic CRS" in str(w.message)]
        self.assertEqual(
            geographic_crs_warnings,
            [],
            "taking a centroid in EPSG:4326 warns; compute it in the projected CRS first",
        )

        expected_lat, expected_lon = self._expected_lat_lon()
        row = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertAlmostEqual(float(row.latitude), expected_lat, places=6)
        self.assertAlmostEqual(float(row.longitude), expected_lon, places=6)


class LoadBrazosGisCommandTests(TestCase):
    def test_skip_download_without_an_existing_archive_raises(self):
        with self.assertRaises(CommandError):
            call_command("load_brazos_gis", "--skip-download", "--skip-extract", "--year", "2099")


class OfflineRerunTests(TestCase):
    """--skip-download must find the archive load_brazos_gis itself downloaded.

    The scrape is what normally supplies the year the archive is named after
    (bcad_gis_<year>.zip). With --skip-download there is no scrape, so the year
    has to come from what is already on disk -- otherwise re-running offline,
    the exact case --skip-download exists for, looks for a filename that was
    never written.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.download_dir = root / "downloads"
        self.extract_root = root / "extracted"
        self.download_dir.mkdir(parents=True)
        self.extract_root.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _settings(self):
        return self.settings(
            BCAD_DOWNLOAD_DIR=str(self.download_dir),
            BCAD_EXTRACT_DIR=str(self.extract_root),
        )

    def _stage_archive(self, year: int) -> None:
        (self.download_dir / f"bcad_gis_{year}.zip").write_bytes(b"not-really-a-zip")

    def _stage_extracted_shapefile(self, year: int) -> None:
        target = self.extract_root / "gis" / str(year)
        target.mkdir(parents=True)
        write_fixture_shapefile(target / "parcels.shp")

    def test_resolves_year_from_the_extracted_directory(self):
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)
        self._stage_archive(2025)
        self._stage_extracted_shapefile(2025)

        with self._settings():
            call_command("load_brazos_gis", "--skip-download", "--skip-extract")

        row = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertEqual(row.situs_address, "5000 SILVER HILL RD")
        self.assertIsNotNone(row.latitude)

    def test_resolves_year_from_the_downloaded_archive_name(self):
        self._stage_archive(2025)

        with self._settings(), self.assertRaises(CommandError) as ctx:
            call_command("load_brazos_gis", "--skip-download", "--skip-extract")

        # The archive resolved; it is the missing .shp that stops us, which
        # proves the label was 2025 and not the "latest" placeholder.
        self.assertIn("No .shp file found", str(ctx.exception))
        self.assertIn("gis/2025", str(ctx.exception))

    def test_picks_the_newest_year_present_on_disk(self):
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        for year in (2024, 2025, 2026):
            self._stage_archive(year)
        self._stage_extracted_shapefile(2026)

        with self._settings():
            call_command("load_brazos_gis", "--skip-download", "--skip-extract")

        self.assertEqual(
            PropertyAccount.objects.get(prop_id="000000010013").situs_address,
            "5000 SILVER HILL RD",
        )

    def test_explicit_year_still_wins_over_what_is_on_disk(self):
        self._stage_archive(2024)
        self._stage_archive(2025)

        with self._settings(), self.assertRaises(CommandError) as ctx:
            call_command("load_brazos_gis", "--skip-download", "--skip-extract", "--year", "2024")

        self.assertIn("gis/2024", str(ctx.exception))

    def test_extracted_parcels_are_enough_without_the_downloaded_zip(self):
        """Keeping the 100MB archive around is not a precondition for a reload."""
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)
        self._stage_extracted_shapefile(2025)  # no _stage_archive

        with self._settings():
            call_command("load_brazos_gis", "--skip-download", "--skip-extract")

        self.assertIsNotNone(PropertyAccount.objects.get(prop_id="000000010013").latitude)

    def test_error_names_the_real_path_when_nothing_is_on_disk(self):
        with self._settings(), self.assertRaises(CommandError) as ctx:
            call_command("load_brazos_gis", "--skip-download")

        message = str(ctx.exception)
        self.assertIn("no BCAD GIS archive", message)
        # --force cannot help when the download step is skipped entirely.
        self.assertNotIn("--force", message)
