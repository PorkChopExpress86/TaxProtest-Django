"""counties/common/analysis.py: pure equity/ordering helpers, no DB needed."""

from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from counties.common.analysis import sort_comps_for_display
from counties.common.contracts import Comp


def _comp(
    key: str,
    *,
    similarity_score: float,
    distance: float | None = 1.0,
    assessed_value: Decimal | float | None = None,
    living_area: float | None = None,
) -> Comp:
    return Comp(
        key=key,
        address=f"{key} Main St",
        similarity_score=similarity_score,
        match_label="Best match",
        distance=distance,
        assessed_value=assessed_value,
        living_area=living_area,
    )


class SortCompsForDisplayTests(SimpleTestCase):
    def test_best_score_first(self):
        low = _comp("A", similarity_score=50.0)
        high = _comp("B", similarity_score=90.0)

        ordered = sort_comps_for_display([low, high])

        self.assertEqual([c.key for c in ordered], ["B", "A"])

    def test_ties_on_score_break_by_nearest_distance(self):
        far = _comp("A", similarity_score=80.0, distance=5.0)
        near = _comp("B", similarity_score=80.0, distance=1.0)

        ordered = sort_comps_for_display([far, near])

        self.assertEqual([c.key for c in ordered], ["B", "A"])

    def test_ties_on_score_and_distance_break_by_cheapest_per_sqft(self):
        expensive = _comp(
            "A",
            similarity_score=80.0,
            distance=1.0,
            assessed_value=Decimal("400000"),
            living_area=1000,
        )
        cheap = _comp(
            "B",
            similarity_score=80.0,
            distance=1.0,
            assessed_value=Decimal("200000"),
            living_area=1000,
        )

        ordered = sort_comps_for_display([expensive, cheap])

        self.assertEqual([c.key for c in ordered], ["B", "A"])

    def test_missing_distance_sorts_last_within_its_tie_group(self):
        # A missing distance must be treated as worst-case, the same way a
        # missing value_per_sqft is, not as "nearest possible" -- otherwise an
        # unscored distance would jump the queue ahead of every real one.
        unknown = _comp("A", similarity_score=80.0, distance=None)
        known = _comp("B", similarity_score=80.0, distance=1.0)

        ordered = sort_comps_for_display([unknown, known])

        self.assertEqual([c.key for c in ordered], ["B", "A"])

    def test_missing_value_per_sqft_sorts_last_within_its_tie_group(self):
        # No assessed_value/living_area -> value_per_sqft is None, treated as
        # infinitely expensive so a real number always beats "unknown".
        unknown = _comp("A", similarity_score=80.0, distance=1.0)
        known = _comp(
            "B",
            similarity_score=80.0,
            distance=1.0,
            assessed_value=Decimal("200000"),
            living_area=1000,
        )

        ordered = sort_comps_for_display([unknown, known])

        self.assertEqual([c.key for c in ordered], ["B", "A"])

    def test_full_tie_breaks_by_key(self):
        z = _comp("Z", similarity_score=80.0, distance=1.0)
        a = _comp("A", similarity_score=80.0, distance=1.0)

        ordered = sort_comps_for_display([z, a])

        self.assertEqual([c.key for c in ordered], ["A", "Z"])

    def test_does_not_mutate_the_input_list(self):
        low = _comp("A", similarity_score=50.0)
        high = _comp("B", similarity_score=90.0)
        original = [low, high]

        sort_comps_for_display(original)

        self.assertEqual([c.key for c in original], ["A", "B"])
