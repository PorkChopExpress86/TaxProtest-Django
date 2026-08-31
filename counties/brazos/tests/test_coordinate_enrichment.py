"""Contract tests for guarded, coordinate-only Brazos enrichment."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from counties.brazos.coordinate_enrichment import (
    BrazosCoordinateEnrichment,
    CoordinateEnrichmentOutcome,
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
    ) -> tuple[CoordinateEnrichmentRequest, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        download_dir = root / "downloads"
        extract_root = root / "extracted"
        shapefile_path = extract_root / "gis" / str(source_year) / "parcels.shp"
        shapefile_path.parent.mkdir(parents=True)
        write_fixture_shapefile(shapefile_path)
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

    def test_apply_publishes_coordinate_updates_and_audit_together(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        request, _ = self._request()

        result = BrazosCoordinateEnrichment().apply(request, minimum_match_rate=0.5)

        self.assertEqual(result.outcome, CoordinateEnrichmentOutcome.APPLIED)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.cleanup_state, CoordinateCleanupState.RETAINED)
        audit = CoordinateEnrichmentAudit.objects.get(pk=result.audit_id)
        self.assertEqual(audit.outcome, CoordinateEnrichmentOutcome.APPLIED)
        account.refresh_from_db()
        self.assertEqual(account.coordinate_source_year, 2025)

    def test_existing_equal_provenance_is_a_noop_not_an_overwrite(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2026,
            latitude=Decimal("30.5000000"),
            longitude=Decimal("-96.5000000"),
            coordinate_source="bcad-certified-gis",
            coordinate_source_year=2025,
        )
        request, _ = self._request()

        result = BrazosCoordinateEnrichment().apply(request, minimum_match_rate=0.5)

        self.assertEqual(result.outcome, CoordinateEnrichmentOutcome.NOOP)
        account.refresh_from_db()
        self.assertEqual(account.latitude, Decimal("30.5000000"))

    def test_gate_decline_is_a_rejected_audited_outcome(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        PropertyAccount.objects.create(prop_id="000000999999", tax_year=2026)
        request, _ = self._request()

        result = BrazosCoordinateEnrichment().apply(request, minimum_match_rate=1.0)

        self.assertEqual(result.outcome, CoordinateEnrichmentOutcome.REJECTED)
        self.assertIn("below", result.reason)
        account.refresh_from_db()
        self.assertIsNone(account.latitude)
