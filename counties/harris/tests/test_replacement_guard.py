from __future__ import annotations

from django.test import SimpleTestCase

from counties.harris.replacement_guard import (
    MIN_REPLACEMENT_RATIO,
    ReplacementShrinkError,
    check_replacement,
)


class CheckReplacementTests(SimpleTestCase):
    def test_threshold_is_half_of_the_rows_replaced(self):
        self.assertEqual(MIN_REPLACEMENT_RATIO, 0.5)

    def test_reload_at_the_threshold_passes(self):
        check_replacement("year", replacing=100, staged=50)

    def test_reload_just_below_the_threshold_is_refused(self):
        with self.assertRaises(ReplacementShrinkError) as ctx:
            check_replacement("PropertyJurisdictionExemption 2025", replacing=100, staged=49)
        self.assertIn("PropertyJurisdictionExemption 2025", str(ctx.exception))
        self.assertIn("100", str(ctx.exception))
        self.assertIn("49", str(ctx.exception))

    def test_reload_may_grow(self):
        check_replacement("year", replacing=100, staged=1_000)

    def test_first_load_into_an_empty_year_passes(self):
        check_replacement("year", replacing=0, staged=1)

    def test_staging_nothing_is_refused_even_when_nothing_is_replaced(self):
        with self.assertRaises(ReplacementShrinkError):
            check_replacement("year", replacing=0, staged=0)
