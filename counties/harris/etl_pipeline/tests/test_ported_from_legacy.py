"""Tests ported from the legacy ``test_residential_etl.py`` and
``test_load_gis_data.py`` suites to cover edge cases the pipeline
integration tests didn't yet exercise.

These cover:
- Condo/multifamily/auxiliary state-class exclusion (not just A1 vs F1)
- ``refresh_property_readiness`` three-tier logic (rooms + building + GIS)
- ``FixturesAggregator`` bedroom/bathroom counting + not-found tracking
- ``load_gis_parcels`` account-map linking, non-residential exclusion,
  last-row-wins on duplicate accounts, and centroid-before-reprojection
  CRS ordering
"""

from __future__ import annotations

import os
import tempfile
import warnings
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from counties.common.tax_models import ParcelGeometry
from counties.harris.etl_pipeline.fixtures_aggregator import FixturesAggregator
from counties.harris.etl_pipeline.gis_loader import load_gis_parcels
from counties.harris.etl_pipeline.readiness import refresh_property_readiness
from counties.harris.models import BuildingDetail, PropertyRecord
from counties.harris.residential import is_residential_state_class


class StateClassExclusionTests(TestCase):
    """Ported from test_bulk_load_properties_excludes_condo_and_multifamily_rows.

    The pipeline integration test only covers A1 vs F1; this test verifies
    that Z4 (condo), B1 (multifamily), and A3 (auxiliary) are also excluded
    at the residential-classification level.
    """

    def test_condo_multifamily_and_auxiliary_classes_are_not_residential(self) -> None:
        self.assertTrue(is_residential_state_class("A1"))
        self.assertTrue(is_residential_state_class("A2"))
        self.assertTrue(is_residential_state_class("A4"))
        self.assertTrue(is_residential_state_class("E1"))
        self.assertFalse(is_residential_state_class("A3"))
        self.assertFalse(is_residential_state_class("B1"))
        self.assertFalse(is_residential_state_class("B2"))
        self.assertFalse(is_residential_state_class("Z1"))
        self.assertFalse(is_residential_state_class("Z4"))


class RefreshPropertyReadinessTests(TestCase):
    """Ported from test_refresh_property_readiness_requires_rooms_building_and_gis.

    The readiness logic moved from etl.py to etl_pipeline/readiness.py;
    this test verifies the three-tier requirement (rooms + building + GIS
    coordinates) and non-residential exclusion still hold.
    """

    def _create_property(
        self, *, account_number, with_gis=True, state_class="A1"
    ) -> PropertyRecord:
        prop = PropertyRecord.objects.create(
            address=f"{account_number} ST",
            city="Houston",
            zipcode="77001",
            value=Decimal("250000"),
            account_number=account_number,
            owner_name="Owner",
            assessed_value=Decimal("245000"),
            building_area=Decimal("2000"),
            land_area=Decimal("8000"),
            state_class=state_class,
            is_residential=state_class != "F1",
        )
        if with_gis:
            ParcelGeometry.objects.create(
                account_number=account_number,
                county="harris",
                latitude=Decimal("29.7600000"),
                longitude=Decimal("-95.3700000"),
            )
        return prop

    def _create_building(self, prop, with_rooms=True) -> BuildingDetail:
        return BuildingDetail.objects.create(
            property=prop,
            account_number=prop.account_number,
            building_number=1,
            quality_code="C",
            condition_code="AV",
            year_built=2005,
            heat_area=Decimal("2000"),
            bedrooms=3 if with_rooms else None,
            bathrooms=Decimal("2.0") if with_rooms else None,
            is_active=True,
        )

    def test_readiness_requires_rooms_building_and_gis(self) -> None:
        ready_prop = self._create_property(account_number="READY001")
        self._create_building(ready_prop)

        missing_gis_prop = self._create_property(account_number="WAIT001", with_gis=False)
        self._create_building(missing_gis_prop)

        non_residential = self._create_property(account_number="OFFICE001", state_class="F1")
        self._create_building(non_residential)

        results = refresh_property_readiness()

        ready_prop.refresh_from_db()
        missing_gis_prop.refresh_from_db()
        non_residential.refresh_from_db()

        self.assertEqual(results["ready_properties_set"], 1)
        self.assertTrue(ready_prop.is_data_ready)
        self.assertFalse(missing_gis_prop.is_data_ready)
        self.assertFalse(non_residential.is_data_ready)

    def test_refresh_only_writes_rows_whose_readiness_changes(self) -> None:
        """The refresh is delta-only: a second run must write nothing.

        The previous implementation cleared every ready row and set them all
        again, so a no-op re-run still rewrote the whole table (measured at
        538s on 1.17M rows). Re-running must now report zero changes while
        leaving the same rows ready.
        """
        ready_prop = self._create_property(account_number="READY001")
        self._create_building(ready_prop)
        self._create_property(account_number="WAIT001", with_gis=False)

        first = refresh_property_readiness()
        self.assertEqual(first["ready_properties_changed"], 1)
        self.assertEqual(first["ready_properties_set"], 1)

        second = refresh_property_readiness()
        self.assertEqual(second["ready_properties_changed"], 0)
        self.assertEqual(second["ready_properties_cleared"], 0)
        # ...and the answer is unchanged, not merely unwritten.
        self.assertEqual(second["ready_properties_set"], 1)
        ready_prop.refresh_from_db()
        self.assertTrue(ready_prop.is_data_ready)

    def test_refresh_clears_a_property_that_stopped_qualifying(self) -> None:
        ready_prop = self._create_property(account_number="READY001")
        building = self._create_building(ready_prop)
        refresh_property_readiness()
        ready_prop.refresh_from_db()
        self.assertTrue(ready_prop.is_data_ready)

        # Room data goes away -> the property is no longer data-ready.
        building.bedrooms = None
        building.bathrooms = None
        building.save(update_fields=["bedrooms", "bathrooms"])

        results = refresh_property_readiness()

        self.assertEqual(results["ready_properties_cleared"], 1)
        self.assertEqual(results["ready_properties_set"], 0)
        ready_prop.refresh_from_db()
        self.assertFalse(ready_prop.is_data_ready)


class FixturesAggregatorTests(TestCase):
    """Ported from test_load_fixtures_room_counts_bulk_updates_and_not_found_tracking.

    The fixtures loading logic moved from etl.py's load_fixtures_room_counts
    to etl_pipeline/fixtures_aggregator.py's FixturesAggregator. This test
    verifies the aggregation math (full + half baths, bedroom counts) and
    the not-found tracking behavior.
    """

    def _create_fixtures_file(self, rows: list[str]) -> Path:
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".txt")
        handle.write("acct\tbld_num\ttype\ttype_dscr\tunits\n")
        for row in rows:
            handle.write(row + "\n")
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return Path(handle.name)

    def test_aggregator_counts_bedrooms_and_baths(self) -> None:
        prop = PropertyRecord.objects.create(
            address="3 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="ACC3",
            state_class="A1",
            is_residential=True,
        )
        BuildingDetail.objects.create(
            property=prop,
            account_number="ACC3",
            building_number=1,
            is_active=True,
        )
        BuildingDetail.objects.create(
            property=prop,
            account_number="ACC3",
            building_number=2,
            is_active=True,
        )

        path = self._create_fixtures_file(
            [
                "ACC3\t1\tRMB\tRoom: Bedroom\t4.00",
                "ACC3\t1\tRMF\tRoom: Full Bath\t2.00",
                "ACC3\t1\tRMH\tRoom: Half Bath\t1.00",
                "ACC3\t2\tRMB\tRoom: Bedroom\t3.00",
                "ACC3\t2\tRMF\tRoom: Full Bath\t1.00",
                "NOPE\t1\tRMB\tRoom: Bedroom\t2.00",
            ]
        )

        agg = FixturesAggregator()
        agg.load_fixtures_file(path)

        self.assertEqual(agg.get_bedroom_count("ACC3", 1), 4)
        self.assertEqual(agg.get_bathroom_count("ACC3", 1), 2.5)
        self.assertEqual(agg.get_fixtures("ACC3", 1)["half_baths"], 1.0)

        self.assertEqual(agg.get_bedroom_count("ACC3", 2), 3)
        self.assertEqual(agg.get_bathroom_count("ACC3", 2), 1.0)

        # Account in the fixtures file but not in the DB is still cached by
        # FixturesAggregator (it doesn't check against PropertyRecord — that
        # filtering happens later in the model_loader when applying fixtures
        # to BuildingDetail rows). An account not in the cache returns zeros:
        self.assertEqual(agg.get_bedroom_count("MISSING", 1), 0)
        self.assertEqual(agg.get_bathroom_count("MISSING", 1), 0.0)

        stats = agg.get_stats()
        self.assertEqual(stats["total_buildings"], 3)


class _FakeCentroid:
    def __init__(self, rows):
        self.x = [row["x"] for row in rows]
        self.y = [row["y"] for row in rows]


class _FakeGeometry:
    def __init__(self, rows):
        self.centroid = _FakeCentroid(rows)


class _FakeCRS:
    def to_epsg(self):
        return 4326


class _FakeGDF:
    """Minimal stand-in for a geopandas GeoDataFrame of parcel polygons."""

    def __init__(self, rows):
        self._rows = rows
        self.columns = ["ACCT", "PARCEL_ID"]
        self.crs = _FakeCRS()
        self.geometry = _FakeGeometry(rows)
        self._derived = {}

    def __len__(self):
        return len(self._rows)

    def __setitem__(self, key, value):
        self._derived[key] = value
        if key in ("latitude", "longitude"):
            for row, v in zip(self._rows, value):
                row[key] = v

    def __getitem__(self, key):
        if key == "centroid":
            return self._derived.get("centroid", self.geometry.centroid)
        return self._derived.get(key)

    def to_crs(self, epsg):
        return self

    def itertuples(self, index=False):
        for row in self._rows:
            yield SimpleNamespace(
                ACCT=row["ACCT"],
                PARCEL_ID=row["PARCEL_ID"],
                latitude=row.get("latitude"),
                longitude=row.get("longitude"),
            )


class LoadGisParcelsTests(TestCase):
    """Ported from test_load_gis_parcels_updates_records_with_account_map.

    The GIS loading logic moved from etl.py to etl_pipeline/gis_loader.py.
    This test verifies account-map linking, non-residential exclusion,
    and last-row-wins behaviour when an account appears twice.
    """

    @patch("counties.harris.etl_pipeline.gis_loader.gpd.read_file")
    @patch("counties.harris.etl_pipeline.gis_loader.GEOPANDAS_AVAILABLE", True)
    def test_load_gis_parcels_updates_records_with_account_map(self, mocked_read_file) -> None:
        prop1 = PropertyRecord.objects.create(
            address="4 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="GIS1",
            state_class="A1",
            is_residential=True,
        )
        prop2 = PropertyRecord.objects.create(
            address="5 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="GIS2",
            state_class="A1",
            is_residential=True,
        )
        non_res = PropertyRecord.objects.create(
            address="6 COMMERCE ST",
            city="Houston",
            zipcode="77002",
            account_number="GIS_NON",
            state_class="F1",
            is_residential=False,
        )

        mocked_read_file.return_value = _FakeGDF(
            [
                {"ACCT": "GIS1", "PARCEL_ID": "P1", "x": -95.1, "y": 29.1},
                {"ACCT": "GIS2", "PARCEL_ID": "P2", "x": -95.2, "y": 29.2},
                {"ACCT": "GIS2", "PARCEL_ID": "P2B", "x": -95.25, "y": 29.25},
                {"ACCT": "GIS_NON", "PARCEL_ID": "PNR", "x": -95.26, "y": 29.26},
                {"ACCT": "MISSING", "PARCEL_ID": "P3", "x": -95.3, "y": 29.3},
                {"ACCT": "", "PARCEL_ID": "P4", "x": -95.4, "y": 29.4},
            ]
        )

        updated = load_gis_parcels("fake.shp", chunk_size=2, refresh_readiness=False)

        # 4 unique accounts with valid ACCT are upserted into ParcelGeometry
        # (GIS2's duplicate row is collapsed by the account-keyed dict, last value wins).
        self.assertEqual(updated, 4)

        from counties.common.tax_models import ParcelGeometry

        geom1 = ParcelGeometry.objects.get(account_number="GIS1", county="harris")
        self.assertAlmostEqual(float(geom1.longitude), -95.1, places=4)
        # GIS2 appears twice; the account-keyed dict keeps the last row's
        # coordinates, not the first.
        geom2 = ParcelGeometry.objects.get(account_number="GIS2", county="harris")
        self.assertAlmostEqual(float(geom2.longitude), -95.25, places=4)
        # Non-residential accounts also get geometry. They are kept out of the
        # comparables search by the property-backed filter that runs *before*
        # the candidate cap — see GeometryCandidateCapTests.
        self.assertTrue(
            ParcelGeometry.objects.filter(account_number="GIS_NON", county="harris").exists()
        )


class CentroidCrsTests(TestCase):
    """Ported from test_load_gis_data.py's CentroidCrsTests.

    Verifies that parcel centroids are computed in the shapefile's
    projected CRS before reprojecting to WGS84, avoiding the geopandas
    "Geometry is in a geographic CRS" warning.
    """

    def _write_polygon_shapefile(self, path: Path):
        import geopandas as gpd
        from shapely.geometry import Polygon

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

        from counties.common.tax_models import ParcelGeometry

        record = ParcelGeometry.objects.get(account_number="GISCRS001", county="harris")
        self.assertAlmostEqual(float(record.latitude), float(expected.y.iloc[0]), places=6)
        self.assertAlmostEqual(float(record.longitude), float(expected.x.iloc[0]), places=6)


class GisCopyEscapingTests(TestCase):
    """Ids carrying COPY metacharacters must survive the staging-table load.

    The Postgres path streams rows into a temp table as COPY text, where a raw
    tab ends a column and a raw backslash starts an escape — an unescaped id
    would shift every column after it on that row, or abort the COPY.
    """

    @patch("counties.harris.etl_pipeline.gis_loader.gpd.read_file")
    @patch("counties.harris.etl_pipeline.gis_loader.GEOPANDAS_AVAILABLE", True)
    def test_tabs_and_backslashes_in_account_numbers_round_trip(self, mocked_read_file) -> None:
        # The account number is the only string the loader writes now, and it is
        # the conflict key -- a shifted column here would corrupt the upsert
        # target rather than just a descriptive field.
        mocked_read_file.return_value = _FakeGDF(
            [
                {"ACCT": "A\tB", "PARCEL_ID": "", "x": -95.1, "y": 29.1},
                {"ACCT": "C\\D", "PARCEL_ID": "", "x": -95.2, "y": 29.2},
                {"ACCT": "E\nF", "PARCEL_ID": "", "x": -95.3, "y": 29.3},
            ]
        )

        updated = load_gis_parcels("fake.shp", refresh_readiness=False)

        self.assertEqual(updated, 3)
        from counties.common.tax_models import ParcelGeometry

        for account_number, longitude in (("A\tB", -95.1), ("C\\D", -95.2), ("E\nF", -95.3)):
            geom = ParcelGeometry.objects.get(account_number=account_number, county="harris")
            self.assertAlmostEqual(float(geom.longitude), longitude, places=4)
