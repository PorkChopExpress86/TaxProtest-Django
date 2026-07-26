"""Tests for the Brazos CAD GIS loader."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.core.management.base import CommandError
from django.test import TestCase

from data.management.commands.load_brazos_gis import Command
from data.models import PropertyAccount


def make_command() -> Command:
    command = Command()
    command.verbosity = 0
    command.dry_run = False
    return command


GIS_PAGE = """
<html><body>
  <a href="/wp-content/uploads/2026/05/BrazosCADParcels_20260422.zip">2025 Certified Shapefiles Download</a>
  <a href="/wp-content/uploads/2024/08/Public_Parcel_Boundary_certified.zip">2024 Certified Shapefiles Download</a>
  <a href="/wp-content/uploads/2023/08/Website_Parcel_Boundary.zip">Brazos County 2021 Certified Shapefiles Download</a>
  <a href="/wp-content/uploads/2024/03/PUBLIC_PARCEL_BOUNDARY.zip">Brazos County Monthly Shapefiles Download</a>
  <a href="/wp-content/uploads/2023/08/Map_Books-08-02-19.zip">Brazos County Map Book Download</a>
  <a href="/wp-content/uploads/2023/08/1916_Map_Books.zip">1916 Map Book</a>
  <a href="https://www.qgis.org/download.html">QGIS</a>
</body></html>
"""


class PortalScrapingTests(TestCase):
    def scrape(self, html: str = GIS_PAGE) -> dict[int, str]:
        response = mock.Mock(text=html)
        response.raise_for_status = mock.Mock()
        with mock.patch(
            "data.management.commands.load_brazos_gis.requests.get", return_value=response
        ):
            return make_command()._fetch_portal()

    def test_finds_certified_shapefiles_by_year(self):
        archives = self.scrape()
        self.assertEqual(sorted(archives), [2021, 2024, 2025])
        self.assertTrue(archives[2025].endswith("BrazosCADParcels_20260422.zip"))

    def test_year_comes_from_link_text_not_the_upload_path(self):
        # The 2025 file lives under /2026/05/ and is named 20260422; only the
        # anchor text states which roll year it belongs to.
        self.assertIn("/2026/05/", self.scrape()[2025])

    def test_skips_map_books(self):
        # Scanned map images, not parcel boundaries.
        archives = self.scrape()
        self.assertFalse(any("Map_Book" in url for url in archives.values()))
        self.assertNotIn(1916, archives)

    def test_skips_non_certified_and_non_zip_links(self):
        archives = self.scrape()
        self.assertFalse(any("PUBLIC_PARCEL_BOUNDARY" in url for url in archives.values()))
        self.assertFalse(any("qgis" in url.lower() for url in archives.values()))

    def test_empty_page_raises(self):
        with self.assertRaisesRegex(CommandError, "No certified shapefiles"):
            self.scrape("<html><body>nothing here</body></html>")


class YearInferenceTests(TestCase):
    def test_infers_year_from_name(self):
        self.assertEqual(make_command()._infer_year("brazos_2024_parcels.zip"), 2024)

    def test_unparseable_name_raises(self):
        with self.assertRaisesRegex(CommandError, "Could not infer a year"):
            make_command()._infer_year("parcels.zip")


class PropIdColumnTests(TestCase):
    """Layer schemas vary year to year; the id column is chosen from the data."""

    def test_finds_all_plausible_id_columns(self):
        # The 2024 parcel layer carries three, holding different representations.
        fields = ["OBJECTID", "XREF_ID", "MULTIPLE_C", "PROP_ID1", "PROP_ID", "prop_id_1"]
        self.assertEqual(Command._prop_id_columns(fields), ["PROP_ID1", "PROP_ID", "prop_id_1"])

    def test_ignores_unrelated_columns(self):
        self.assertEqual(Command._prop_id_columns(["SUBD_ID", "SHAPE_STAr", "TextString"]), [])

    def test_geo_id_is_not_treated_as_a_property_id(self):
        # XREF_ID holds '191000-0122-0010'; digit-stripping it would fabricate ids.
        self.assertEqual(Command._prop_id_columns(["XREF_ID", "geo_id"]), [])

    def test_parses_bare_integer_ids(self):
        self.assertEqual(Command._parse_prop_id(22549), 22549)

    def test_parses_r_prefixed_string_ids(self):
        # The 2024 layer spells the same parcel 'R22549'.
        self.assertEqual(Command._parse_prop_id("R22549"), 22549)

    def test_rejects_unusable_ids(self):
        for value in (None, "", "   ", "R", 0, "0"):
            self.assertIsNone(Command._parse_prop_id(value), f"{value!r} should be unusable")


class ApplyTests(TestCase):
    """The COPY + UPDATE path that attaches coordinates to accounts."""

    def setUp(self):
        PropertyAccount.objects.create(prop_id=10002, tax_year=2025, prop_type_cd="R")
        PropertyAccount.objects.create(prop_id=38698, tax_year=2025, prop_type_cd="R")
        # Mineral account: no parcel boundary exists for it.
        PropertyAccount.objects.create(prop_id=99999, tax_year=2025, prop_type_cd="MN")
        # Same parcel id, different year — must not be touched.
        PropertyAccount.objects.create(prop_id=10002, tax_year=2024, prop_type_cd="R")

    def test_updates_matching_accounts_only(self):
        points = [(10002, 30.714218, -96.342518, 60728.0), (38698, 30.553794, -96.274011, 8761.0)]
        updated, matched = make_command()._apply(points, 2025)
        self.assertEqual(updated, 2)
        self.assertEqual(matched, 2)

        located = PropertyAccount.objects.get(prop_id=10002, tax_year=2025)
        self.assertEqual(located.latitude, Decimal("30.7142180"))
        self.assertEqual(located.longitude, Decimal("-96.3425180"))
        self.assertEqual(located.parcel_area_sqft, Decimal("60728.00"))
        self.assertTrue(located.has_location)

    def test_leaves_other_tax_years_untouched(self):
        make_command()._apply([(10002, 30.7, -96.3, 100.0)], 2025)
        other = PropertyAccount.objects.get(prop_id=10002, tax_year=2024)
        self.assertIsNone(other.latitude)
        self.assertFalse(other.has_location)

    def test_accounts_without_a_parcel_stay_null(self):
        make_command()._apply([(10002, 30.7, -96.3, 100.0)], 2025)
        mineral = PropertyAccount.objects.get(prop_id=99999, tax_year=2025)
        self.assertIsNone(mineral.latitude)
        self.assertFalse(mineral.has_location)

    def test_parcels_with_no_matching_account_are_ignored(self):
        # The shapefile covers parcels that may not be in this year's roll.
        updated, _ = make_command()._apply([(555555, 30.7, -96.3, 10.0)], 2025)
        self.assertEqual(updated, 0)

    def test_missing_area_is_stored_as_null(self):
        make_command()._apply([(10002, 30.7, -96.3, None)], 2025)
        self.assertIsNone(
            PropertyAccount.objects.get(prop_id=10002, tax_year=2025).parcel_area_sqft
        )

    def test_rerunning_is_idempotent(self):
        points = [(10002, 30.714218, -96.342518, 60728.0)]
        make_command()._apply(points, 2025)
        updated, _ = make_command()._apply(points, 2025)
        self.assertEqual(updated, 1)
        self.assertEqual(
            PropertyAccount.objects.filter(tax_year=2025, latitude__isnull=False).count(), 1
        )

    def test_year_with_no_accounts_raises(self):
        with self.assertRaisesRegex(CommandError, "Run load_brazos_cad first"):
            make_command()._apply([(10002, 30.7, -96.3, 1.0)], 1999)

    def test_dry_run_reports_coverage_without_writing(self):
        command = make_command()
        command.dry_run = True
        updated, matched = command._apply(
            [(10002, 30.7, -96.3, 1.0), (777, 30.7, -96.3, 1.0)], 2025
        )
        self.assertEqual(updated, 0)
        self.assertEqual(matched, 1)
        self.assertIsNone(PropertyAccount.objects.get(prop_id=10002, tax_year=2025).latitude)


class RepresentativePointTests(TestCase):
    """Why the loader uses representative_point() rather than centroid."""

    def test_centroid_can_fall_outside_a_concave_parcel(self):
        from shapely.geometry import Polygon

        # A C-shaped parcel, the shape of a river-front or cul-de-sac lot.
        c_shape = Polygon([(0, 0), (3, 0), (3, 1), (1, 1), (1, 2), (3, 2), (3, 3), (0, 3)])
        self.assertFalse(c_shape.contains(c_shape.centroid))
        # representative_point is guaranteed to land inside, so a parcel's point
        # can never be attributed to a neighbouring property.
        self.assertTrue(c_shape.contains(c_shape.representative_point()))
