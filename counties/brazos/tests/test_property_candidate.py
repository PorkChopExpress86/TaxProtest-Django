"""Brazos candidate preparation never changes the active detailed snapshot."""

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount, SnapshotOutcome
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
)
from counties.brazos.tests.test_property_import import _stage_complete_pacs_export
from counties.common.models import ImportCandidate
from counties.common.tax_models import PropertyJurisdictionExemption


class BrazosCandidateTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.filter(county="brazos"):
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def run_candidate(self, root, mode=PropertyImportMode.CAD_RECOVERY):
        with self.settings(
            BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
        ):
            return BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(
                PropertyImportRequest(
                    mode=mode,
                    options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
                    prepare_only=True,
                )
            )

    def baseline(self):
        snapshot = BrazosPropertySnapshot.objects.create(
            tax_year=2026,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2026,
            gis_source_year=2026,
        )
        PropertyAccount.objects.create(
            tax_year=2026, prop_id="000000010013", owner_name="Published owner"
        )
        return snapshot

    def test_same_year_partial_candidate_is_durable_and_keeps_shared_reads(self):
        old = self.baseline()
        harris = PropertyJurisdictionExemption.objects.create(
            county="harris",
            account_number="H1",
            tax_year=2026,
            tax_unit_code="H",
            taxable_value=100,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _stage_complete_pacs_export(root, 2026)
            result = self.run_candidate(root)
            self.assertTrue(result.prepared)
            self.assertIsNone(result.snapshot_id)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.state, "prepared")
            self.assertEqual(candidate.baseline["snapshot_id"], old.pk)
            self.assertEqual(candidate.evidence["outcome"], "partial")
            self.assertTrue(candidate.sources)
            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT COUNT(*) FROM "{candidate.storage_schema}".data_propertyjurisdictionexemption WHERE county = %s',
                    ["harris"],
                )
                self.assertEqual(cursor.fetchone()[0], 0)
            self.assertEqual(
                PropertyJurisdictionExemption.objects.get(county="harris").pk, harris.pk
            )
            self.assertEqual(PropertyAccount.objects.get().owner_name, "Published owner")
            self.assertContains(
                self.client.get(reverse("brazos_index"), {"owner_name": "Published"}),
                "Published owner",
            )
        connection.close()
        candidate.refresh_from_db()
        self.client.force_login(
            get_user_model().objects.create_superuser("reviewer", password="test")
        )
        self.assertContains(
            self.client.get(reverse("admin:data_importcandidate_change", args=[candidate.pk])),
            "GIS capabilities unavailable",
        )

    def test_annual_gis_failure_retains_blocked_candidate_and_previous_snapshot(self):
        old = self.baseline()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _stage_complete_pacs_export(root, 2026)
            with self.assertRaises(CommandError):
                self.run_candidate(root, PropertyImportMode.ANNUAL)
            candidate = ImportCandidate.objects.get()
            self.assertEqual(candidate.state, "blocked")
            self.assertEqual(candidate.request["mode"], "annual")
            self.assertTrue(candidate.sources)
            self.assertEqual(BrazosPropertySnapshot.objects.get(is_active=True).pk, old.pk)
            self.assertEqual(PropertyAccount.objects.get().owner_name, "Published owner")

    def test_gis_recovery_candidate_records_exact_partial_prerequisite(self):
        import geopandas as gpd
        from shapely.geometry import Point

        old = self.baseline()
        old.outcome = SnapshotOutcome.PARTIAL
        old.gis_source_year = None
        old.save()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shape = root / "extracted" / "gis" / "2026" / "parcels.shp"
            shape.parent.mkdir(parents=True)
            gpd.GeoDataFrame(
                {"PROP_ID": [10013]}, geometry=[Point(3556000, 10120000)], crs="EPSG:2277"
            ).to_file(shape)
            result = self.run_candidate(root, PropertyImportMode.GIS_RECOVERY)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.baseline["snapshot_id"], old.pk)
            self.assertEqual(candidate.baseline["outcome"], "partial")
            self.assertTrue(all(source["source_year"] == 2026 for source in candidate.sources))
            self.assertIsNone(PropertyAccount.objects.get().latitude)
            self.assertEqual(BrazosPropertySnapshot.objects.get(is_active=True).pk, old.pk)
