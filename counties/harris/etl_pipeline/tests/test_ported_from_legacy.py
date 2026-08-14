"""Tests ported from the legacy ``test_residential_etl.py`` and
``test_load_gis_data.py`` suites to cover edge cases the pipeline
integration tests didn't yet exercise.

These cover:
- Condo/multifamily/auxiliary state-class exclusion (not just A1 vs F1)
- ``refresh_property_readiness`` three-tier logic (rooms + building + GIS)
- ``FixturesAggregator`` bedroom/bathroom counting + not-found tracking
- ``load_gis_parcels`` account-map linking, non-residential exclusion,
  parcel-id clobbering, and centroid-before-reprojection CRS ordering
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
        return PropertyRecord.objects.create(
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
            latitude=Decimal("29.7600000") if with_gis else None,
            longitude=Decimal("-95.3700000") if with_gis else None,
        )

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


class LoadGisParcelsTests(TestCase):
    """Ported from test_load_gis_parcels_updates_records_with_account_map.

    The GIS loading logic moved from etl.py to etl_pipeline/gis_loader.py.
    This test verifies account-map linking, non-residential exclusion,
    and parcel-id clobbering behavior.
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

        self.assertEqual(updated, 2)
        prop1.refresh_from_db()
        prop2.refresh_from_db()
        non_res.refresh_from_db()
        self.assertEqual(prop1.parcel_id, "P1")
        self.assertEqual(prop2.parcel_id, "P2B")
        self.assertIsNone(non_res.latitude)
        self.assertIsNone(non_res.longitude)
        self.assertNotEqual(non_res.parcel_id, "PNR")


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

        record = PropertyRecord.objects.get(account_number="GISCRS001")
        self.assertAlmostEqual(float(record.latitude), float(expected.y.iloc[0]), places=6)
        self.assertAlmostEqual(float(record.longitude), float(expected.x.iloc[0]), places=6)
