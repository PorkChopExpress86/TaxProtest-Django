"""Contract tests for the explicitly stale Brazos coordinate fallback."""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.core.management.base import CommandError
from django.test import TestCase

from counties.brazos.annual_refresh import RefreshOptions, StagePreparation
from counties.brazos.coordinate_enrichment import BrazosCoordinateEnrichment
from counties.brazos.gis_refresh import GisSourcePayload
from counties.brazos.models import PropertyAccount
from counties.brazos.tests.test_load_brazos_gis import write_fixture_shapefile


class _PreparedGisSource:
    name = "gis"

    def __init__(self, shapefile_path: Path, *, source_year: int, target_year: int):
        self._preparation = StagePreparation(
            name=self.name,
            source_year=source_year,
            target_year=target_year,
            payload=GisSourcePayload(
                shapefile_path=shapefile_path, extract_dir=shapefile_path.parent
            ),
            cleanup_paths=(shapefile_path.parent,),
        )

    def prepare(self, options: RefreshOptions) -> StagePreparation:
        return self._preparation


class CoordinateEnrichmentTests(TestCase):
    def _analyze_fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shapefile_path = Path(tmp.name) / "parcels.shp"
        write_fixture_shapefile(shapefile_path)
        enrichment = BrazosCoordinateEnrichment(
            source_stage=_PreparedGisSource(shapefile_path, source_year=2025, target_year=2026)
        )
        return enrichment.analyze(RefreshOptions(tax_year=2026))

    def test_measurement_and_apply_update_only_coordinates_with_provenance(self):
        matched = PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2026,
            situs_address="Certified CAD situs",
            state_class="E1",
        )
        unmatched = PropertyAccount.objects.create(prop_id="000000999999", tax_year=2026)

        _, report, candidates = self._analyze_fixture()

        self.assertEqual(report.source_year, 2025)
        self.assertEqual(report.target_year, 2026)
        self.assertEqual(report.source_records, 2)
        self.assertEqual(report.matched_accounts, 1)
        self.assertEqual(report.unmatched_target_accounts, 1)
        self.assertEqual(report.unmatched_source_ids, 1)
        self.assertEqual(report.match_rate, 0.5)

        self.assertEqual(
            BrazosCoordinateEnrichment().apply(report, candidates, minimum_match_rate=0.5), 1
        )

        matched.refresh_from_db()
        unmatched.refresh_from_db()
        self.assertIsNotNone(matched.latitude)
        self.assertIsNotNone(matched.longitude)
        self.assertEqual(matched.coordinate_source, "bcad-certified-gis")
        self.assertEqual(matched.coordinate_source_year, 2025)
        self.assertEqual(matched.situs_address, "Certified CAD situs")
        self.assertEqual(matched.state_class, "E1")
        self.assertIsNone(unmatched.latitude)
        self.assertIsNone(unmatched.coordinate_source_year)

    def test_apply_requires_an_explicit_coverage_threshold(self):
        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        _, report, candidates = self._analyze_fixture()

        with self.assertRaisesRegex(CommandError, "minimum-match-rate"):
            BrazosCoordinateEnrichment().apply(report, candidates, minimum_match_rate=None)

        account.refresh_from_db()
        self.assertIsNone(account.latitude)
        self.assertIsNone(account.coordinate_source_year)

    def test_apply_rejects_an_empty_target_year_even_with_a_zero_threshold(self):
        _, report, candidates = self._analyze_fixture()

        with self.assertRaisesRegex(CommandError, "no PropertyAccount rows"):
            BrazosCoordinateEnrichment().apply(report, candidates, minimum_match_rate=0)

    def test_duplicate_normalized_source_ids_block_apply(self):
        import geopandas as gpd
        from shapely.geometry import Point

        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        with tempfile.TemporaryDirectory() as tmp:
            shapefile_path = Path(tmp) / "parcels.shp"
            gpd.GeoDataFrame(
                {"PROP_ID": [10013, 10013]},
                geometry=[Point(3556000, 10120000), Point(3556100, 10120100)],
                crs="EPSG:2277",
            ).to_file(shapefile_path)
            enrichment = BrazosCoordinateEnrichment(
                source_stage=_PreparedGisSource(shapefile_path, source_year=2025, target_year=2026)
            )
            _, report, candidates = enrichment.analyze(RefreshOptions(tax_year=2026))

        self.assertEqual(report.duplicate_source_ids, 1)
        with self.assertRaisesRegex(CommandError, "duplicated"):
            enrichment.apply(report, candidates, minimum_match_rate=0)

        account.refresh_from_db()
        self.assertIsNone(account.latitude)

    def test_rejects_a_year_matched_source_so_annual_refresh_remains_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            shapefile_path = Path(tmp) / "parcels.shp"
            write_fixture_shapefile(shapefile_path)
            enrichment = BrazosCoordinateEnrichment(
                source_stage=_PreparedGisSource(shapefile_path, source_year=2026, target_year=2026)
            )

            with self.assertRaisesRegex(CommandError, "refresh_brazos_annual"):
                enrichment.analyze(RefreshOptions(tax_year=2026))
