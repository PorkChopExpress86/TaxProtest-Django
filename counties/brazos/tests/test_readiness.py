"""Contract tests for the Brazos active-snapshot/readiness read module."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount, SnapshotOutcome
from counties.brazos.readiness import BrazosActiveSnapshotReadiness


class ActiveSnapshotReadTests(TestCase):
    def test_projection_uses_the_published_snapshot_not_the_latest_account_row(self):
        prior = BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
            is_active=False,
        )
        active = BrazosPropertySnapshot.objects.create(
            tax_year=2024,
            outcome=SnapshotOutcome.PARTIAL,
            cad_source_year=2024,
        )
        PropertyAccount.objects.create(
            prop_id="000000010013", tax_year=prior.tax_year, owner_name="Newer row"
        )
        PropertyAccount.objects.create(
            prop_id="000000010013", tax_year=active.tax_year, owner_name="Published row"
        )

        projection = BrazosActiveSnapshotReadiness().project("000000010013")

        self.assertEqual(projection.tax_year, 2024)
        self.assertEqual(projection.owner_name, "Published row")
        self.assertTrue(projection.search_ready)

    def test_search_and_projection_share_trimmed_readiness_and_coordinate_provenance(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.PARTIAL,
            cad_source_year=2025,
        )
        PropertyAccount.objects.create(
            prop_id="whitespace-only",
            tax_year=2025,
            owner_name="  ",
            situs_address=" ",
            mailing_address=" ",
        )
        PropertyAccount.objects.create(
            prop_id="source-backed",
            tax_year=2025,
            owner_name="Owner",
            coordinate_source="bcad-certified-gis",
            coordinate_source_year=2025,
        )

        readiness = BrazosActiveSnapshotReadiness()

        self.assertFalse(readiness.project_static("whitespace-only").search_ready)
        self.assertEqual(
            list(readiness.search_queryset().values_list("prop_id", flat=True)), ["source-backed"]
        )
        projection = readiness.project_static("source-backed")
        self.assertEqual(projection.coordinate_source, "bcad-certified-gis")
        self.assertEqual(projection.coordinate_source_year, 2025)

    def test_projection_uses_one_captured_active_snapshot(self):
        snapshot = BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.PARTIAL,
            cad_source_year=2025,
        )
        PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2025,
            owner_name="Owner",
        )
        readiness = BrazosActiveSnapshotReadiness()

        with patch.object(
            readiness, "active_snapshot", wraps=readiness.active_snapshot
        ) as active_snapshot:
            projection = readiness.project("000000010013")

        self.assertEqual(active_snapshot.call_count, 1)
        self.assertEqual(projection.snapshot_id, snapshot.id)
