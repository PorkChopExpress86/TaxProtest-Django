"""Tests for counties/common/tax_evaluation.py."""

from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from counties.common.tax_evaluation import (
    evaluate_assessment_history,
    evaluate_tax_impact,
    history_availability_notice,
    year_over_year_percent,
)
from counties.common.tax_models import AssessmentHistory


class YearOverYearPercentTests(SimpleTestCase):
    def test_percentage_increase(self):
        result = year_over_year_percent(110, 100)
        self.assertEqual(result, Decimal("10.00"))

    def test_percentage_decrease(self):
        result = year_over_year_percent(90, 100)
        self.assertEqual(result, Decimal("-10.00"))

    def test_prior_is_none_returns_none(self):
        self.assertIsNone(year_over_year_percent(100, None))

    def test_prior_is_zero_returns_none(self):
        self.assertIsNone(year_over_year_percent(100, 0))

    def test_current_is_none_returns_none(self):
        self.assertIsNone(year_over_year_percent(None, 100))


class HistoryAvailabilityNoticeTests(SimpleTestCase):
    def test_empty_history_returns_unavailable_notice(self):
        notice = history_availability_notice([], 2026)
        self.assertIn("unavailable", notice)

    def test_no_gaps_returns_empty_string(self):
        history = [
            {"tax_year": 2026, "assessed_value": 100},
            {"tax_year": 2025, "assessed_value": 90},
            {"tax_year": 2024, "assessed_value": 80},
        ]
        notice = history_availability_notice(history, 2026)
        self.assertEqual(notice, "")

    def test_gaps_reported_in_notice(self):
        history = [
            {"tax_year": 2026, "assessed_value": 100},
            {"tax_year": 2024, "assessed_value": 80},
        ]
        notice = history_availability_notice(history, 2026)
        self.assertIn("2025", notice)


class EvaluateAssessmentHistoryTests(TestCase):
    def test_evaluates_history_ordered_by_tax_year(self):
        AssessmentHistory.objects.create(
            county="harris",
            account_number="ACC123",
            tax_year=2025,
            assessed_value=Decimal("100000"),
            appraised_value=Decimal("100000"),
            market_value=Decimal("100000"),
        )
        AssessmentHistory.objects.create(
            county="harris",
            account_number="ACC123",
            tax_year=2026,
            assessed_value=Decimal("110000"),
            appraised_value=Decimal("110000"),
            market_value=Decimal("110000"),
        )

        rows = evaluate_assessment_history("harris", "ACC123", limit=5, has_typed_cap_flag=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tax_year"], 2026)
        self.assertEqual(rows[0]["increase_percent"], Decimal("10.00"))
        self.assertIn("cap_status", rows[0])
        self.assertEqual(rows[1]["tax_year"], 2025)
        self.assertIsNone(rows[1]["increase_percent"])


class EvaluateTaxImpactTests(TestCase):
    def test_evaluates_tax_impact_delegation(self):
        result = evaluate_tax_impact("harris", "NON_EXISTENT", 2026, Decimal("300000"))
        self.assertEqual(result.tax_year, 2026)
        self.assertIn("missing", result.completeness)
