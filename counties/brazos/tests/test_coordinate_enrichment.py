"""Contract tests for guarded, coordinate-only Brazos enrichment."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management.base import CommandError
from django.test import TestCase

from counties.brazos.annual_refresh import RefreshOptions, StagePreparation
from counties.brazos.coordinate_enrichment import (
    BrazosCoordinateEnrichment,
    CoordinateEnrichmentOutcome,
    CoordinateEnrichmentRequest,
)
from counties.brazos.gis_refresh import GisSourcePayload
from counties.brazos.models import (
    BrazosPropertySnapshot,
    CoordinateCleanupState,
    CoordinateEnrichmentAudit,
    PropertyAccount,
    SnapshotOutcome,
)
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
        self.cleaned = False

    def prepare(self, options: RefreshOptions) -> StagePreparation:
        return self._preparation

    def cleanup(self, preparation: StagePreparation) -> None:
        self.cleaned = True


class CoordinateEnrichmentTests(TestCase):
    def _enrichment(self, *, source_year: int = 2025, target_year: int = 2026):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shapefile_path = Path(tmp.name) / "parcels.shp"
        write_fixture_shapefile(shapefile_path)
        source = _PreparedGisSource(
            shapefile_path, source_year=source_year, target_year=target_year
        )
        return BrazosCoordinateEnrichment(source_stage=source), source

    @staticmethod
    def _active_partial_snapshot(tax_year: int = 2026) -> BrazosPropertySnapshot:
        return BrazosPropertySnapshot.objects.create(
            tax_year=tax_year,
            outcome=SnapshotOutcome.PARTIAL,
            cad_source_year=tax_year,
        )

    def test_analysis_records_evidence_without_updating_property_coordinates(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        enrichment, source = self._enrichment()

        result = enrichment.run(CoordinateEnrichmentRequest(options=RefreshOptions(tax_year=2026)))

        self.assertEqual(result.outcome, CoordinateEnrichmentOutcome.ANALYZED)
        self.assertEqual(result.updated_count, 0)
        self.assertEqual(result.cleanup_state, CoordinateCleanupState.RETAINED)
        self.assertFalse(source.cleaned)
        audit = CoordinateEnrichmentAudit.objects.get(pk=result.audit_id)
        self.assertEqual(audit.outcome, CoordinateEnrichmentOutcome.ANALYZED)
        self.assertEqual(audit.evidence["matched_accounts"], 1)
        account.refresh_from_db()
        self.assertIsNone(account.latitude)

    def test_apply_publishes_coordinate_updates_and_audit_together(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        enrichment, source = self._enrichment()

        result = enrichment.run(
            CoordinateEnrichmentRequest(
                options=RefreshOptions(tax_year=2026), apply=True, minimum_match_rate=0.5
            )
        )

        self.assertEqual(result.outcome, CoordinateEnrichmentOutcome.APPLIED)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.cleanup_state, CoordinateCleanupState.CLEANED)
        self.assertTrue(source.cleaned)
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
        enrichment, _ = self._enrichment()

        result = enrichment.run(
            CoordinateEnrichmentRequest(
                options=RefreshOptions(tax_year=2026), apply=True, minimum_match_rate=0.5
            )
        )

        self.assertEqual(result.outcome, CoordinateEnrichmentOutcome.NOOP)
        account.refresh_from_db()
        self.assertEqual(account.latitude, Decimal("30.5000000"))

    def test_gate_decline_is_a_rejected_audited_outcome(self):
        self._active_partial_snapshot()
        account = PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        PropertyAccount.objects.create(prop_id="000000999999", tax_year=2026)
        enrichment, _ = self._enrichment()

        result = enrichment.run(
            CoordinateEnrichmentRequest(
                options=RefreshOptions(tax_year=2026), apply=True, minimum_match_rate=1.0
            )
        )

        self.assertEqual(result.outcome, CoordinateEnrichmentOutcome.REJECTED)
        self.assertIn("below", result.reason)
        account.refresh_from_db()
        self.assertIsNone(account.latitude)

    def test_year_matched_gis_is_rejected_before_an_audit_or_write(self):
        self._active_partial_snapshot()
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026)
        enrichment, _ = self._enrichment(source_year=2026)

        with self.assertRaisesRegex(CommandError, "refresh_brazos_annual"):
            enrichment.run(CoordinateEnrichmentRequest(options=RefreshOptions(tax_year=2026)))

        self.assertFalse(CoordinateEnrichmentAudit.objects.exists())
