from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from data.models import AssessmentHistory, PropertyJurisdictionExemption, TaxUnitRate
from data.tax_impact import calculate_tax_impact


class TaxImpactCalculatorTests(TestCase):
    def setUp(self):
        AssessmentHistory.objects.create(
            account_number="TAX001",
            tax_year=2026,
            assessed_value=Decimal("300000"),
        )

    def test_complete_inputs_compute_exact_totals(self):
        TaxUnitRate.objects.create(
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            adopted_rate=Decimal("0.020000"),
        )
        TaxUnitRate.objects.create(
            tax_year=2026,
            tax_unit_code="U2",
            tax_unit_name="Unit 2",
            adopted_rate=Decimal("0.010000"),
        )

        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            taxable_value=Decimal("300000"),
            exemption_code="HS",
            exemption_amount=Decimal("40000"),
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U2",
            tax_unit_name="Unit 2",
            taxable_value=Decimal("300000"),
            exemption_code="OV65",
            exemption_percent=Decimal("10"),
        )

        result = calculate_tax_impact("TAX001", 2026, Decimal("250000"))

        self.assertEqual(result.completeness, "complete")
        self.assertEqual(result.current_tax_owed, Decimal("7900.00"))
        self.assertEqual(result.median_tax_owed, Decimal("6450.00"))
        self.assertEqual(result.estimated_savings, Decimal("1450.00"))
        self.assertFalse(result.warnings)

    def test_partial_inputs_emit_warnings_and_partial_totals(self):
        TaxUnitRate.objects.create(
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            adopted_rate=Decimal("0.020000"),
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            taxable_value=Decimal("300000"),
            exemption_code="",
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U2",
            tax_unit_name="Unit 2",
            taxable_value=Decimal("300000"),
            exemption_code="",
        )

        result = calculate_tax_impact("TAX001", 2026, Decimal("250000"))

        self.assertEqual(result.completeness, "partial")
        self.assertEqual(result.current_tax_owed, Decimal("6000.00"))
        self.assertTrue(result.warnings)
        self.assertEqual(len(result.per_unit_breakdown), 2)

    def test_missing_median_value_does_not_fabricate_full_bill_savings(self):
        TaxUnitRate.objects.create(
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            adopted_rate=Decimal("0.020000"),
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            taxable_value=Decimal("300000"),
            exemption_code="",
        )

        result = calculate_tax_impact("TAX001", 2026, None)

        self.assertEqual(result.completeness, "partial")
        self.assertEqual(result.current_tax_owed, Decimal("6000.00"))
        self.assertIsNone(result.median_tax_owed)
        self.assertIsNone(result.estimated_savings)
        self.assertTrue(result.warnings)
        # The per-unit breakdown must not fabricate a $0.00 median figure either -- it
        # should mirror the None-ness of the aggregate, not silently default to zero.
        self.assertEqual(len(result.per_unit_breakdown), 1)
        self.assertIsNone(result.per_unit_breakdown[0]["median_taxable_value"])
        self.assertIsNone(result.per_unit_breakdown[0]["median_tax_amount"])
        self.assertEqual(result.per_unit_breakdown[0]["current_tax_amount"], Decimal("6000.00"))

    def test_jurisdiction_data_present_but_no_rates_anywhere_reports_missing(self):
        """Mirrors the real production gap: PropertyJurisdictionExemption is fully populated for
        the latest year, but TaxUnitRate has zero rows for any year, so no fallback year can
        possibly help -- the whole feature must report "missing" with no fabricated totals,
        while still returning one per-unit breakdown row per taxing unit."""
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            taxable_value=Decimal("300000"),
            exemption_code="",
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U2",
            tax_unit_name="Unit 2",
            taxable_value=Decimal("300000"),
            exemption_code="",
        )

        result = calculate_tax_impact("TAX001", 2026, Decimal("250000"))

        self.assertEqual(result.completeness, "missing")
        self.assertIsNone(result.current_tax_owed)
        self.assertIsNone(result.median_tax_owed)
        self.assertIsNone(result.estimated_savings)
        self.assertIsNone(result.effective_rate)
        self.assertEqual(len(result.per_unit_breakdown), 2)
        for row in result.per_unit_breakdown:
            self.assertEqual(row["warning"], "Missing tax-unit rate")

    def test_no_jurisdiction_data_reports_missing_with_no_fabricated_totals(self):
        result = calculate_tax_impact("TAX001", 2026, Decimal("250000"))

        self.assertEqual(result.completeness, "missing")
        self.assertIsNone(result.current_tax_owed)
        self.assertIsNone(result.median_tax_owed)
        self.assertIsNone(result.estimated_savings)
        self.assertIsNone(result.effective_rate)
        self.assertEqual(result.per_unit_breakdown, [])

    def test_no_assessment_year_reports_missing_with_no_fabricated_totals(self):
        result = calculate_tax_impact("NO_SUCH_ACCOUNT", None, Decimal("250000"))

        self.assertEqual(result.completeness, "missing")
        self.assertIsNone(result.tax_year)
        self.assertIsNone(result.current_tax_owed)

    def test_falls_back_to_latest_year_with_usable_rate_and_exemption_data(self):
        AssessmentHistory.objects.create(
            account_number="TAX001",
            tax_year=2027,
            assessed_value=Decimal("310000"),
        )
        # 2027's roll is certified (jurisdiction/exemption rows exist) but the taxing unit
        # hasn't adopted a rate yet -- only 2026 has both.
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2027,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            taxable_value=Decimal("310000"),
            exemption_code="",
        )
        TaxUnitRate.objects.create(
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            adopted_rate=Decimal("0.020000"),
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U1",
            tax_unit_name="Unit 1",
            taxable_value=Decimal("300000"),
            exemption_code="",
        )

        result = calculate_tax_impact("TAX001", None, Decimal("250000"))

        self.assertEqual(result.tax_year, 2026)
        self.assertEqual(result.current_tax_owed, Decimal("6000.00"))
        self.assertTrue(any("2027" in w and "2026" in w for w in result.warnings))

    def test_exemption_precedence_fixed_then_percent(self):
        TaxUnitRate.objects.create(
            tax_year=2026,
            tax_unit_code="U1",
            adopted_rate=Decimal("0.020000"),
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U1",
            taxable_value=Decimal("300000"),
            exemption_code="A",
            exemption_amount=Decimal("10000"),
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="TAX001",
            tax_year=2026,
            tax_unit_code="U1",
            taxable_value=Decimal("300000"),
            exemption_code="B",
            exemption_percent=Decimal("10"),
        )

        result = calculate_tax_impact("TAX001", 2026, Decimal("250000"))

        self.assertEqual(result.current_tax_owed, Decimal("5220.00"))
        self.assertEqual(result.median_tax_owed, Decimal("4320.00"))
        self.assertEqual(result.estimated_savings, Decimal("900.00"))
