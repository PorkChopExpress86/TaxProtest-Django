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

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from brazos_cad.management.commands.load_brazos_gis import Command
from brazos_cad.models import PropertyAccount

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
            "brazos_cad.management.commands.load_brazos_gis.requests.get",
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
            "brazos_cad.management.commands.load_brazos_gis.requests.get",
            return_value=_mock_response(html),
        ):
            with self.assertRaises(CommandError):
                Command()._scrape_archive("https://brazoscad.org/tax-information/gis/")


class LoadGisDataTests(TestCase):
    def _write_fixture_shapefile(self, path: Path) -> None:
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


class LoadBrazosGisCommandTests(TestCase):
    def test_skip_download_without_an_existing_archive_raises(self):
        with self.assertRaises(CommandError):
            call_command("load_brazos_gis", "--skip-download", "--skip-extract", "--year", "2099")
