"""Contract tests for the Brazos active-snapshot/readiness read module."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from counties.brazos.adapter import adapter
from counties.brazos.models import (
    BrazosPropertySnapshot,
    PropertyAccount,
    PropertyLand,
    SnapshotOutcome,
)
from counties.brazos.readiness import BrazosActiveSnapshotReadiness
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate

TARGET = "000000010013"


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


class BrazosTaxReadinessTests(TestCase):
    """Tax readiness names the exact missing input, and units that levy nothing need no rate."""

    def setUp(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
        )
        for index, prop_id in enumerate([TARGET, "000000020001", "000000020002", "000000020003"]):
            PropertyAccount.objects.create(
                prop_id=prop_id,
                tax_year=2025,
                owner_name=f"OWNER {index}",
                situs_address=f"{100 + index} MAIN ST",
                situs_zip="77801",
                latitude=Decimal("30.6700000") + Decimal(index) / Decimal("100000"),
                longitude=Decimal("-96.3700000"),
                coordinate_source="bcad-certified-gis",
                coordinate_source_year=2025,
                living_area=Decimal("2000"),
                assessed_value=Decimal("300000") if index == 0 else Decimal("250000"),
                class_code="RV3",
            )
            PropertyLand.objects.create(
                prop_id=prop_id, tax_year=2025, land_seq=1, acreage=Decimal("0.2500")
            )
        TaxUnitRate.objects.create(
            county="brazos", tax_year=2025, tax_unit_code="S1", adopted_rate=Decimal("0.011")
        )

    def unit(self, code, name, exemption_code="", amount=None):
        PropertyJurisdictionExemption.objects.create(
            county="brazos",
            tax_year=2025,
            account_number=TARGET,
            tax_unit_code=code,
            tax_unit_name=name,
            exemption_code=exemption_code,
            exemption_amount=amount,
            taxable_value=None if exemption_code else Decimal("300000"),
        )

    def test_units_that_levy_no_tax_need_no_rate(self):
        self.unit("S1", "BRYAN ISD")
        self.unit("CAD", "BRAZOS CENTRAL APPRAISAL DISTRICT")
        self.unit("ZRFND", "BCAD REFUND")
        self.unit("TZ21B", "BRYAN TAX INCREMENT ZONE #21")
        projection = BrazosActiveSnapshotReadiness().project(TARGET)
        self.assertTrue(projection.tax_impact_ready, projection.reason_for("tax"))
        tax = adapter.tax_impact(TARGET, 2025, Decimal("250000"))
        self.assertEqual(tax.completeness, "complete")
        self.assertEqual(tax.current_tax_owed, Decimal("3300.00"))
        warnings = {row["tax_unit_code"]: row["warning"] for row in tax.per_unit_breakdown}
        self.assertIn("levies no tax", warnings["TZ21B"])
        self.assertIn("levies no tax", warnings["CAD"])

    def test_a_property_without_matching_year_rows_says_which_year(self):
        self.assertEqual(
            BrazosActiveSnapshotReadiness().project(TARGET).reason_for("tax"),
            "No 2025 jurisdiction and exemption rows for this property",
        )

    def test_report_names_each_missing_tax_input(self):
        TaxUnitRate.objects.create(
            county="brazos", tax_year=2025, tax_unit_code="G1", adopted_rate=Decimal("0.004")
        )
        self.unit("S1", "BRYAN ISD")
        self.unit("S2", "COLLEGE STATION ISD")
        self.unit("G1", "BRAZOS COUNTY")
        self.unit("G1", "BRAZOS COUNTY", exemption_code="UNVERIFIED")
        self.unit("W9", "", exemption_code="HS", amount=Decimal("100000"))
        gaps = (
            "2025 exemption amounts are unverified for taxing unit G1 (BRAZOS COUNTY); "
            "2025 gross jurisdiction base unavailable for taxing unit W9; "
            "Adopted 2025 rate unavailable for taxing units S2 (COLLEGE STATION ISD), W9"
        )
        self.assertEqual(BrazosActiveSnapshotReadiness().project(TARGET).reason_for("tax"), gaps)
        response = self.client.get(reverse("brazos_protest_analysis", args=[TARGET]))
        self.assertEqual(response.context["tax_impact"].completeness, "missing")
        self.assertContains(response, "COLLEGE STATION ISD")
        self.assertEqual(response.context["tax_impact"].warnings, [gaps])
