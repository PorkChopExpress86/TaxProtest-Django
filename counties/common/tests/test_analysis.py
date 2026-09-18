"""counties/common/analysis.py: pure equity/ordering helpers, no DB needed."""

from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from counties.common.analysis import build_protest_dossier, sort_comps_for_display
from counties.common.contracts import (
    Comp,
    CountyAdapter,
    CountyProfile,
    PropertyCapabilities,
    Subject,
)


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


class FakeAdapter(CountyAdapter):
    profile = CountyProfile(
        slug="test",
        display_name="Test County",
        district_abbr="TCAD",
        district_name="Test County Appraisal District",
        key_label="Account",
        url_prefix="test/",
        url_name_prefix="test_",
        search_fields=(),
        search_columns=(),
        comp_columns=(),
    )

    def __init__(
        self,
        subject: Subject | None = None,
        caps: PropertyCapabilities | None = None,
        comps: list[Comp] | None = None,
    ):
        self._subject = subject
        self._caps = caps or PropertyCapabilities()
        self._comps = comps or []

    def search_queryset(self, params):
        return []

    def search_rows(self, records):
        return []

    def get_subject(self, key: str) -> Subject | None:
        return self._subject

    def capabilities(self, key: str) -> PropertyCapabilities:
        return self._caps

    def find_comps(
        self, key: str, *, max_distance_miles: float, max_results: int, min_score: float
    ) -> list[Comp]:
        return [c for c in self._comps if (c.similarity_score or 0) >= min_score]

    def assessment_history(self, key: str, limit: int = 5):
        return [{"tax_year": 2026, "assessed_value": 300000}]

    def tax_impact(self, key: str, tax_year: int | None, median_assessed_value: Decimal | None):
        return None


class BuildProtestDossierTests(SimpleTestCase):
    def test_unknown_property_returns_unavailable(self):
        adapter = FakeAdapter(subject=None)
        outcome = build_protest_dossier(adapter, "missing")

        self.assertFalse(outcome.is_ready)
        self.assertEqual(outcome.error, "Property not found")

    def test_not_report_ready_returns_unavailable_with_reason(self):
        subject = Subject(
            key="1",
            address_line="123 Main",
            has_location=True,
        )
        caps = PropertyCapabilities(report_ready=False, reasons={"report": "Need 3 comps"})
        adapter = FakeAdapter(subject=subject, caps=caps)

        outcome = build_protest_dossier(adapter, "1")

        self.assertFalse(outcome.is_ready)
        self.assertEqual(outcome.error, "Need 3 comps")
        self.assertEqual(outcome.subject, subject)

    def test_missing_location_returns_unavailable(self):
        subject = Subject(
            key="1",
            address_line="123 Main",
            has_location=False,
        )
        adapter = FakeAdapter(subject=subject)

        outcome = build_protest_dossier(adapter, "1")

        self.assertFalse(outcome.is_ready)
        self.assertIn("location data", outcome.error)
        self.assertEqual(outcome.subject, subject)

    def test_ready_subject_builds_complete_dossier(self):
        subject = Subject(
            key="1",
            address_line="123 Main",
            has_location=True,
            assessed_value=Decimal("300000"),
            living_area=2000,
            tax_year=2026,
        )
        comps = [
            _comp(
                "C1",
                similarity_score=85.0,
                distance=0.5,
                assessed_value=Decimal("250000"),
                living_area=2000,
            )
        ]
        adapter = FakeAdapter(subject=subject, comps=comps)

        outcome = build_protest_dossier(adapter, "1", min_score="75")

        self.assertTrue(outcome.is_ready)
        self.assertIsNotNone(outcome.dossier)
        dossier = outcome.dossier
        self.assertEqual(dossier.min_score, 75.0)
        self.assertEqual(len(dossier.comps), 1)
        self.assertEqual(len(dossier.comp_rows), 1)
        self.assertIsNotNone(dossier.comp_rows[0].delta)
        self.assertIsNotNone(dossier.comp_rows[0].breakdown_summary)
        self.assertEqual(dossier.subject.key, "1")
        self.assertEqual(outcome.subject, subject)
