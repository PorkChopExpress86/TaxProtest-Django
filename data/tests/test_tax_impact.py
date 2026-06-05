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
