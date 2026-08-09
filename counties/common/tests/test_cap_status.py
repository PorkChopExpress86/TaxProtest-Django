"""evaluate_cap_status: county-neutral math over a per-county flag vocabulary.

Harris rows carry HCAD's own Y/N/Pending Cap_acct flag. Brazos rows carry a
*derived* flag (see import_brazos_assessment_history's docstring: "Y" only
means a capping reduction was applied that year, not which cap type) -- these
tests pin that the two are read differently, not run through the same
homestead-vs-circuit-breaker guess.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from counties.common.cap_status import evaluate_cap_status
from counties.harris.models import AssessmentHistory


class HarrisTypedFlagTests(TestCase):
    """HCAD's Cap_acct is a Y/N/Pending flag, never blank on real rows.

    The real 2022-2026 load is 5.5M "N", 2.3M "Y", 26k "Pending" -- so a
    non-emptiness test puts every property under the 10% homestead cap. These
    pin the flag's actual values rather than the empty string no real row uses.
    """

    def _pair(self, flag: str, *, county: str = "harris"):
        prior = AssessmentHistory.objects.create(
            account_number=f"CAPFLAG{flag or 'BLANK'}",
            tax_year=2025,
            county=county,
            assessed_value=Decimal("400000"),
            appraised_value=Decimal("400000"),
            market_value=Decimal("430000"),
        )
        current = AssessmentHistory.objects.create(
            account_number=f"CAPFLAG{flag or 'BLANK'}",
            tax_year=2026,
            county=county,
            assessed_value=Decimal("470000"),
            appraised_value=Decimal("470000"),
            market_value=Decimal("600000"),
            prior_appraised_value=Decimal("400000"),
            new_construction_value=Decimal("0"),
            cap_account=flag,
        )
        return evaluate_cap_status(current, prior)

    def test_homestead_uses_ten_percent_plus_new_construction(self):
        prior = AssessmentHistory.objects.create(
            account_number="HIST003",
            tax_year=2025,
            assessed_value=Decimal("300000"),
            appraised_value=Decimal("300000"),
            market_value=Decimal("330000"),
        )
        current = AssessmentHistory.objects.create(
            account_number="HIST003",
            tax_year=2026,
            assessed_value=Decimal("341000"),
            appraised_value=Decimal("341000"),
            market_value=Decimal("380000"),
            prior_appraised_value=Decimal("300000"),
            new_construction_value=Decimal("10000"),
            cap_account="Y",
        )

        status = evaluate_cap_status(current, prior)

        self.assertEqual(status["cap_type"], "homestead")
        self.assertEqual(status["limit_percent"], Decimal("10"))
        self.assertEqual(status["allowed_value"], Decimal("340000.00"))
        self.assertEqual(status["status"], "over_limit")
        self.assertEqual(status["increase_percent"], Decimal("13.67"))

    def test_non_homestead_circuit_breaker_uses_twenty_percent(self):
        prior = AssessmentHistory.objects.create(
            account_number="HIST004",
            tax_year=2025,
            assessed_value=Decimal("400000"),
            appraised_value=Decimal("400000"),
            market_value=Decimal("430000"),
        )
        current = AssessmentHistory.objects.create(
            account_number="HIST004",
            tax_year=2026,
            assessed_value=Decimal("470000"),
            appraised_value=Decimal("470000"),
            market_value=Decimal("600000"),
            prior_appraised_value=Decimal("400000"),
            new_construction_value=Decimal("0"),
            cap_account="",
        )

        status = evaluate_cap_status(current, prior)

        self.assertEqual(status["cap_type"], "circuit_breaker")
        self.assertEqual(status["limit_percent"], Decimal("20"))
        self.assertEqual(status["allowed_value"], Decimal("480000.00"))
        self.assertEqual(status["status"], "within_limit")
        self.assertEqual(status["increase_percent"], Decimal("17.50"))

    def test_y_is_the_homestead_cap(self):
        status = self._pair("Y")
        self.assertEqual(status["cap_type"], "homestead")
        self.assertEqual(status["limit_percent"], Decimal("10"))
        # 400k +10% = 440k, so a 470k appraisal is over the cap.
        self.assertEqual(status["status"], "over_limit")

    def test_n_is_the_circuit_breaker_not_the_homestead_cap(self):
        status = self._pair("N")
        self.assertEqual(status["cap_type"], "circuit_breaker")
        self.assertEqual(status["limit_percent"], Decimal("20"))
        # 400k +20% = 480k, so the same 470k appraisal is within the cap.
        self.assertEqual(status["status"], "within_limit")

    def test_pending_does_not_claim_the_tighter_homestead_cap(self):
        status = self._pair("Pending")
        self.assertEqual(status["cap_type"], "circuit_breaker")

    def test_flag_comparison_is_case_insensitive(self):
        self.assertEqual(self._pair("y")["cap_type"], "homestead")

    def test_unexpected_values_fall_back_to_the_circuit_breaker(self):
        # A handful of real rows carry stray numerics from a misaligned column.
        self.assertEqual(self._pair("0.0539")["cap_type"], "circuit_breaker")


class NonTypedCountyFlagTests(TestCase):
    """Brazos's cap_account is derived (appraised > assessed), not HCAD's flag.

    evaluate_cap_status must not read Brazos's "Y" as "homestead cap" -- it
    doesn't know that, and asserting a 10%/20% limit it can't back up would
    put a fabricated number in front of the ARB. Confirms the county branch
    added alongside issue #14's flag-hardwiring finding.
    """

    def _pair(self, flag: str):
        prior = AssessmentHistory.objects.create(
            account_number=f"BCAPFLAG{flag or 'BLANK'}",
            tax_year=2025,
            county="brazos",
            assessed_value=Decimal("400000"),
            appraised_value=Decimal("400000"),
            market_value=Decimal("430000"),
        )
        current = AssessmentHistory.objects.create(
            account_number=f"BCAPFLAG{flag or 'BLANK'}",
            tax_year=2026,
            county="brazos",
            assessed_value=Decimal("470000"),
            appraised_value=Decimal("470000"),
            market_value=Decimal("600000"),
            prior_appraised_value=Decimal("400000"),
            new_construction_value=Decimal("0"),
            cap_account=flag,
        )
        return evaluate_cap_status(current, prior)

    def test_derived_y_flag_does_not_assert_homestead(self):
        status = self._pair("Y")
        self.assertEqual(status["cap_type"], "unknown")
        self.assertIsNone(status["limit_percent"])
        self.assertEqual(status["status"], "unknown")
        self.assertEqual(status["label"], "Needs review")

    def test_derived_blank_flag_also_stays_unknown(self):
        # Absence of a cap reduction this year still isn't evidence of WHICH
        # cap regime the property is under -- stay honest either way.
        status = self._pair("")
        self.assertEqual(status["cap_type"], "unknown")
        self.assertIsNone(status["limit_percent"])
        self.assertEqual(status["status"], "unknown")

    def test_year_over_year_increase_percent_is_still_real_data(self):
        # The one thing we DO know for any county: the raw value trend. Don't
        # let an unknown cap type hide a computable, county-neutral figure.
        status = self._pair("Y")
        self.assertEqual(status["increase_percent"], Decimal("17.50"))

    def test_no_allowed_value_or_overage_is_asserted(self):
        status = self._pair("Y")
        self.assertIsNone(status["allowed_value"])
        self.assertIsNone(status["overage"])
