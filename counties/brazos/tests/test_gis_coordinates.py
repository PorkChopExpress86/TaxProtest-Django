"""Focused geospatial evidence tests shared by both Brazos GIS consumers."""

from __future__ import annotations

from django.test import SimpleTestCase

from counties.brazos.gis_coordinates import interpret_gis_coordinates


class GisCoordinateInterpretationTests(SimpleTestCase):
    def test_reports_normalized_duplicate_and_invalid_coordinate_evidence(self):
        import geopandas as gpd
        from shapely.geometry import Point, Polygon

        parcel = Polygon(
            [
                (3556000, 10120000),
                (3557500, 10120000),
                (3557500, 10120400),
                (3556000, 10120400),
            ]
        )
        source = gpd.GeoDataFrame(
            {"PROP_ID": [10013, 10013, None, 10055]},
            geometry=[parcel, Point(3557000, 10120200), Point(0, 0), Point()],
            crs="EPSG:2277",
        )

        evidence = interpret_gis_coordinates(source)

        self.assertEqual(evidence.source_records, 4)
        self.assertEqual(evidence.usable_coordinate_records, 1)
        self.assertEqual(evidence.distinct_source_ids, 2)
        self.assertEqual(evidence.duplicate_source_ids, 1)
        self.assertEqual(evidence.invalid_coordinate_records, 1)
        self.assertEqual(set(evidence.coordinates), {"000000010013"})

        expected = gpd.GeoSeries([Point(3557000, 10120200)], crs="EPSG:2277").to_crs(epsg=4326)
        latitude, longitude = evidence.coordinates["000000010013"]
        self.assertAlmostEqual(float(latitude), float(expected.y.iloc[0]), places=7)
        self.assertAlmostEqual(float(longitude), float(expected.x.iloc[0]), places=7)
