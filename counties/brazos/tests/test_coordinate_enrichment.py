"""Contract tests for guarded, coordinate-only Brazos enrichment."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase

from counties.brazos.coordinate_enrichment import (
    BrazosCoordinateEnrichment,
    CoordinateEnrichmentOutcome,
    CoordinateEnrichmentRejected,
    CoordinateEnrichmentReport,
    CoordinateEnrichmentRequest,
    CoordinateEnrichmentSourceError,
    InvalidCoordinateEnrichmentRequest,
)
from counties.brazos.models import (
    BrazosPropertySnapshot,
    CoordinateCleanupState,
    CoordinateEnrichmentAudit,
    PropertyAccount,
    SnapshotOutcome,
)
from counties.brazos.tests.test_load_brazos_gis import write_fixture_shapefile


class CoordinateEnrichmentTests(TestCase):
    def _request(
        self,
        *,
        source_year: int = 2025,
        target_year: int = 2026,
        source_writer=write_fixture_shapefile,
    ) -> tuple[CoordinateEnrichmentRequest, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        download_dir = root / "downloads"
        extract_root = root / "extracted"
        shapefile_path = extract_root / "gis" / str(source_year) / "parcels.shp"
        shapefile_path.parent.mkdir(parents=True)
        source_writer(shapefile_path)
        override = self.settings(
            BCAD_DOWNLOAD_DIR=str(download_dir),
            BCAD_EXTRACT_DIR=str(extract_root),
        )
        override.enable()
        self.addCleanup(override.disable)
        return (
            CoordinateEnrichmentRequest(
                target_year=target_year,
                expected_source_year=source_year,
                skip_download=True,
                skip_extract=True,
            ),
            shapefile_path,
        )

    @staticmethod
    def _active_partial_snapshot(tax_year: int = 2026) -> BrazosPropertySnapshot:
        return BrazosPropertySnapshot.objects.create(
            tax_year=tax_year,
            outcome=SnapshotOutcome.PARTIAL,
            cad_source_year=tax_year,
        )

    def test_analysis_returns_only_report_without_database_writes_and_retains_source(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        request, shapefile_path = self._request()

        report = BrazosCoordinateEnrichment().analyze(request)

        self.assertIsInstance(report, CoordinateEnrichmentReport)
        self.assertEqual(report.matched_accounts, 1)
        self.assertTrue(shapefile_path.exists())
        self.assertFalse(CoordinateEnrichmentAudit.objects.exists())
        account.refresh_from_db()
        self.assertIsNone(account.latitude)

    def test_invalid_request_has_a_distinct_failure_before_source_access(self):
        request = CoordinateEnrichmentRequest(
            target_year=2026,
            expected_source_year=2026,
            skip_download=True,
            skip_extract=True,
        )

        with self.assertRaisesRegex(
            InvalidCoordinateEnrichmentRequest,
            "refresh_brazos_annual",
        ):
            BrazosCoordinateEnrichment().analyze(request)

    def test_unavailable_source_has_a_distinct_failure(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        request = CoordinateEnrichmentRequest(
            target_year=2026,
            expected_source_year=2025,
            skip_download=True,
            skip_extract=True,
        )

        with (
            self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                BCAD_EXTRACT_DIR=str(root / "extracted"),
            ),
            self.assertRaises(CoordinateEnrichmentSourceError),
        ):
            BrazosCoordinateEnrichment().analyze(request)

    def test_application_remeasures_after_an_earlier_analysis(self):
        self._active_partial_snapshot()
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        request, _ = self._request()
        enrichment = BrazosCoordinateEnrichment()
        earlier_report = enrichment.analyze(request)
        PropertyAccount.objects.create(prop_id="000000999999", tax_year=2026)

        with self.assertRaises(CoordinateEnrichmentRejected) as ctx:
            enrichment.apply(request, minimum_match_rate=1.0)

        self.assertEqual(earlier_report.target_accounts, 1)
        self.assertEqual(ctx.exception.report.target_accounts, 2)
        self.assertEqual(ctx.exception.report.matched_accounts, 1)
        self.assertIn("below", " ".join(ctx.exception.violations).lower())
        self.assertFalse(CoordinateEnrichmentAudit.objects.exists())

    def test_apply_updates_exactly_the_measured_matched_population(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2026,
            owner_name="Original owner",
            situs_address="Original address",
            latitude=Decimal("30.5000000"),
            longitude=Decimal("-96.5000000"),
            coordinate_source="older-source",
            coordinate_source_year=2024,
        )
        request, _ = self._request()

        result = BrazosCoordinateEnrichment().apply(request, minimum_match_rate=0.5)

        self.assertEqual(result.outcome, CoordinateEnrichmentOutcome.APPLIED)
        self.assertEqual(result.updated_count, result.report.matched_accounts)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.cleanup_state, CoordinateCleanupState.RETAINED)
        account.refresh_from_db()
        self.assertEqual(account.coordinate_source, "bcad-certified-gis")
        self.assertEqual(account.coordinate_source_year, 2025)
        self.assertNotEqual(account.latitude, Decimal("30.5000000"))
        self.assertEqual(account.owner_name, "Original owner")
        self.assertEqual(account.situs_address, "Original address")

    def test_zero_matches_reject_even_with_zero_threshold(self):
        self._active_partial_snapshot()
        PropertyAccount.objects.create(prop_id="000000999999", tax_year=2026)
        request, _ = self._request()

        with self.assertRaises(CoordinateEnrichmentRejected) as ctx:
            BrazosCoordinateEnrichment().apply(request, minimum_match_rate=0.0)

        self.assertEqual(ctx.exception.report.matched_accounts, 0)
        self.assertIn("zero", " ".join(ctx.exception.violations).lower())

    def test_empty_target_population_rejects_with_measured_report(self):
        self._active_partial_snapshot()
        request, _ = self._request()

        with self.assertRaises(CoordinateEnrichmentRejected) as ctx:
            BrazosCoordinateEnrichment().apply(request, minimum_match_rate=0.0)

        self.assertEqual(ctx.exception.report.target_accounts, 0)
        self.assertIn("no propertyaccount", " ".join(ctx.exception.violations).lower())

    def test_source_without_usable_coordinates_rejects(self):
        import geopandas as gpd
        from shapely.geometry import Point

        def write_empty_geometry(path: Path) -> None:
            gpd.GeoDataFrame(
                {"PROP_ID": [10013]},
                geometry=[Point()],
                crs="EPSG:2277",
            ).to_file(path)

        self._active_partial_snapshot()
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        request, _ = self._request(source_writer=write_empty_geometry)

        with self.assertRaises(CoordinateEnrichmentRejected) as ctx:
            BrazosCoordinateEnrichment().apply(request, minimum_match_rate=0.0)

        self.assertEqual(ctx.exception.report.usable_coordinate_records, 0)
        self.assertIn("no usable coordinates", " ".join(ctx.exception.violations).lower())

    def test_duplicate_normalized_source_ids_reject(self):
        import geopandas as gpd
        from shapely.geometry import Point

        def write_duplicate_ids(path: Path) -> None:
            gpd.GeoDataFrame(
                {"PROP_ID": [10013, 10013]},
                geometry=[Point(3556000, 10120000), Point(3556100, 10120100)],
                crs="EPSG:2277",
            ).to_file(path)

        self._active_partial_snapshot()
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        request, _ = self._request(source_writer=write_duplicate_ids)

        with self.assertRaises(CoordinateEnrichmentRejected) as ctx:
            BrazosCoordinateEnrichment().apply(request, minimum_match_rate=0.0)

        self.assertEqual(ctx.exception.report.duplicate_source_ids, 1)
        self.assertIn("duplicated", " ".join(ctx.exception.violations).lower())

    def test_invalid_non_finite_threshold_rejects_before_source_access(self):
        request = CoordinateEnrichmentRequest(target_year=2026)

        with self.assertRaises(InvalidCoordinateEnrichmentRequest):
            BrazosCoordinateEnrichment().apply(request, minimum_match_rate=float("nan"))

    def test_persistence_failure_rolls_back_and_retains_source(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        request, shapefile_path = self._request()

        with (
            patch.object(
                PropertyAccount.objects,
                "bulk_update",
                side_effect=DatabaseError("write failed"),
            ),
            self.assertRaises(DatabaseError),
        ):
            BrazosCoordinateEnrichment().apply(request, minimum_match_rate=0.5)

        account.refresh_from_db()
        self.assertIsNone(account.latitude)
        self.assertTrue(shapefile_path.exists())
        self.assertFalse(CoordinateEnrichmentAudit.objects.exists())
