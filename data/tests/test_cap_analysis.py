from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from data.cap_analysis import evaluate_cap_gap
from data.models import AssessmentHistory


def _entry(**kwargs) -> AssessmentHistory:
    defaults = {
        "account_number": "CAP001",
        "tax_year": 2026,
        "cap_account": "Y",
    }
    defaults.update(kwargs)
    return AssessmentHistory.objects.create(**defaults)


class CapGapEvaluationTests(TestCase):
    def test_no_entry_reports_missing(self):
        result = evaluate_cap_gap(None, Decimal("300000"))
        self.assertEqual(result.scenario, "missing")
        self.assertIsNone(result.market_value)

    def test_no_value_data_reports_missing(self):
        entry = _entry(assessed_value=None, appraised_value=None, market_value=None)
        result = evaluate_cap_gap(entry, Decimal("300000"))
        self.assertEqual(result.scenario, "missing")

    def test_no_gap_direct_flow_through(self):
        entry = _entry(
            market_value=Decimal("400000"),
            appraised_value=Decimal("400000"),
            assessed_value=Decimal("400000"),
        )
        result = evaluate_cap_gap(entry, Decimal("360000"), combined_rate=Decimal("0.022"))

        self.assertEqual(result.scenario, "direct")
        self.assertFalse(result.cap_engaged)
        self.assertEqual(result.cap_gap, Decimal("0.00"))
        self.assertEqual(result.current_year_taxable_reduction, Decimal("40000.00"))
        self.assertEqual(result.estimated_current_savings, Decimal("880.00"))

    def test_gap_blocks_current_year_but_helps_next_year(self):
        # Market $462,603, capped $308,092 (the verified WCAD example). Target $330,000
        # sits inside the gap but below next year's ceiling of $338,901.20.
        entry = _entry(
            market_value=Decimal("462603"),
            appraised_value=Decimal("308092"),
            assessed_value=Decimal("462603"),
        )
        result = evaluate_cap_gap(entry, Decimal("330000"), combined_rate=Decimal("0.02"))

        self.assertEqual(result.scenario, "blocked")
        self.assertTrue(result.cap_engaged)
        self.assertEqual(result.cap_gap, Decimal("154511.00"))
        self.assertEqual(result.required_reduction_for_current_savings, Decimal("154511.00"))
        self.assertIsNone(result.current_year_taxable_reduction)
        self.assertEqual(result.next_year_ceiling, Decimal("338901.20"))
        self.assertEqual(result.future_year_taxable_reduction, Decimal("8901.20"))
        self.assertEqual(result.estimated_future_savings, Decimal("178.02"))

    def test_target_below_capped_value_still_saves_despite_gap(self):
        entry = _entry(
            market_value=Decimal("462603"),
            appraised_value=Decimal("308092"),
            assessed_value=Decimal("462603"),
        )
        result = evaluate_cap_gap(entry, Decimal("290000"), combined_rate=Decimal("0.02"))

        self.assertEqual(result.scenario, "effective")
        self.assertEqual(result.current_year_taxable_reduction, Decimal("18092.00"))
        self.assertEqual(result.estimated_current_savings, Decimal("361.84"))

    def test_target_above_next_year_ceiling_is_cosmetic(self):
        entry = _entry(
            market_value=Decimal("462603"),
            appraised_value=Decimal("308092"),
            assessed_value=Decimal("462603"),
        )
        result = evaluate_cap_gap(entry, Decimal("400000"))

        self.assertEqual(result.scenario, "cosmetic")
        self.assertIsNone(result.current_year_taxable_reduction)
        self.assertIsNone(result.future_year_taxable_reduction)

    def test_target_at_or_above_market_is_already_below(self):
        entry = _entry(
            market_value=Decimal("300000"),
            appraised_value=Decimal("300000"),
            assessed_value=Decimal("300000"),
        )
        result = evaluate_cap_gap(entry, Decimal("310000"))
        self.assertEqual(result.scenario, "already_below")

    def test_no_target_reports_gap_only(self):
        entry = _entry(
            market_value=Decimal("462603"),
            appraised_value=Decimal("308092"),
            assessed_value=Decimal("462603"),
        )
        result = evaluate_cap_gap(entry, None)

        self.assertEqual(result.scenario, "no_target")
        self.assertEqual(result.required_reduction_for_current_savings, Decimal("154511.00"))

    def test_non_homestead_uses_circuit_breaker_twenty_percent(self):
        entry = _entry(
            cap_account="N",
            market_value=Decimal("500000"),
            appraised_value=Decimal("450000"),
            assessed_value=Decimal("500000"),
        )
        result = evaluate_cap_gap(entry, None)

        self.assertEqual(result.cap_type, "circuit_breaker")
        self.assertEqual(result.cap_limit_percent, Decimal("20"))
        self.assertEqual(result.next_year_ceiling, Decimal("540000.00"))

    def test_legacy_rows_without_appraised_fall_back_to_assessed(self):
        # Pre-reimport rows have only assessed_value populated.
        entry = _entry(
            cap_account="",
            market_value=None,
            appraised_value=None,
            assessed_value=Decimal("350000"),
        )
        result = evaluate_cap_gap(entry, Decimal("320000"))

        self.assertEqual(result.scenario, "direct")
        self.assertEqual(result.cap_gap, Decimal("0.00"))
        self.assertTrue(result.notes)

    def test_no_rate_means_no_dollar_estimates(self):
        entry = _entry(
            market_value=Decimal("400000"),
            appraised_value=Decimal("400000"),
            assessed_value=Decimal("400000"),
        )
        result = evaluate_cap_gap(entry, Decimal("360000"), combined_rate=None)

        self.assertEqual(result.scenario, "direct")
        self.assertEqual(result.current_year_taxable_reduction, Decimal("40000.00"))
        self.assertIsNone(result.estimated_current_savings)
