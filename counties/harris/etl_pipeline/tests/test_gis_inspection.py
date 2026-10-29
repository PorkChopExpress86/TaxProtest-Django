"""One coordinate per account when HCAD splits a parcel across several shapes."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import geopandas as gpd
from django.test import SimpleTestCase
from shapely.geometry import Point, box

from counties.harris.etl_pipeline.gis_loader import inspect_gis_parcels

_X, _Y = 3100000, 13800000
# A 100 ft square and a disjoint 200 ft square; their union's centroid is area-weighted.
_SMALL = box(_X, _Y, _X + 100, _Y + 100)
_LARGE = box(_X + 200, _Y, _X + 400, _Y + 200)


def _inspect(accounts: list[str], shapes: list) -> object:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "parcels.shp"
        gpd.GeoDataFrame({"ACCT": accounts}, geometry=shapes, crs="EPSG:2278").to_file(path)
        return inspect_gis_parcels(str(path))


class SplitParcelTests(SimpleTestCase):
    def test_a_split_parcel_takes_the_centroid_of_all_its_shapes_and_is_flagged(self) -> None:
        inspection = _inspect(["A", "A", "B"], [_SMALL, _LARGE, box(_X, _Y, _X + 1, _Y + 1)])

        expected = (
            gpd.GeoSeries([Point(_X + 250, _Y + 90)], crs="EPSG:2278").to_crs(epsg=4326).iloc[0]
        )
        latitude, longitude, _ = inspection.coordinates["A"]
        self.assertAlmostEqual(latitude, expected.y, places=9)
        self.assertAlmostEqual(longitude, expected.x, places=9)
        self.assertEqual(inspection.merged_accounts, ("A",))
        self.assertEqual((inspection.invalid, inspection.skipped), (0, 1))

    def test_the_merged_coordinate_does_not_depend_on_file_order(self) -> None:
        forward = _inspect(["A", "A"], [_SMALL, _LARGE])
        backward = _inspect(["A", "A"], [_LARGE, _SMALL])

        self.assertEqual(forward.coordinates, backward.coordinates)

    def test_exact_duplicate_shapes_are_skipped_without_a_merge(self) -> None:
        single = _inspect(["A"], [_SMALL])
        duplicated = _inspect(["A", "A"], [_SMALL, _SMALL])

        self.assertEqual(duplicated.coordinates, single.coordinates)
        self.assertEqual(duplicated.merged_accounts, ())
        self.assertEqual(duplicated.skipped, 1)


class ParcelLayoutTests(SimpleTestCase):
    def test_the_october_layout_keys_shapes_by_hcad_num_1_not_the_stack_parcel(self) -> None:
        # HCAD's October 2026 layer renamed HCAD_NUM to HCAD_NUM_1 and added LOWPARCELID,
        # the lowest account sharing a stacked (condo) shape. Shapes with no account are
        # invalid rather than assigned to the stack's lowest account.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "parcels.shp"
            gpd.GeoDataFrame(
                {"LOWPARCEL": ["A", "A", "C"], "HCAD_NUM_1": ["A", "B", None]},
                geometry=[_SMALL, _LARGE, box(_X, _Y, _X + 1, _Y + 1)],
                crs="EPSG:2278",
            ).to_file(path)
            inspection = inspect_gis_parcels(str(path))

        self.assertEqual(sorted(inspection.coordinates), ["A", "B"])
        self.assertEqual(inspection.coordinates["B"][2], "B")
        self.assertEqual(inspection.invalid, 1)
