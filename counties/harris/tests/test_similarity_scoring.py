"""Harris similarity scoring, exercised through the public search.

Distances come from real coordinates: one thousandth of a degree of latitude
is about 0.069 miles at the shared query's earth radius.
"""

from decimal import Decimal
from unittest import mock

from django.test import TestCase

from counties.common.tests.similarity_scenarios import harris_property
from counties.harris.models import BuildingDetail, PropertyRecord
from counties.harris.similarity import find_similar_properties


def _scores(subject: str) -> dict[str, dict]:
    results = find_similar_properties(subject, max_results=50, min_score=0.0)
    return {result["property"].account_number: result for result in results}


class SimilarityScoringTests(TestCase):
    def test_granular_score_separates_best_from_ok_match(self) -> None:
        harris_property("TARGET0000001", building={})
        harris_property("CAND00000001", lat_offset="0.0058", building={})
        harris_property(
            "CAND00000002",
            lat_offset="0.0695",
            land_area=Decimal("12000"),
            assessed_value=Decimal("320000"),
            building={
                "heat_area": Decimal("2640"),
                "bedrooms": 3,
                "bathrooms": Decimal("2.0"),
                "quality_code": "B",
                "condition_code": "D",
                "year_built": 1994,
                "effective_year": 1996,
                "stories": Decimal("1.0"),
                "building_style": "RN",
            },
        )

        scores = _scores("TARGET0000001")
        perfect_score = scores["CAND00000001"]["similarity_score"]
        ok_match_score = scores["CAND00000002"]["similarity_score"]

        self.assertGreater(perfect_score, 95)
        self.assertGreater(perfect_score, ok_match_score + 25)
        self.assertGreaterEqual(ok_match_score, 35)
        self.assertLess(ok_match_score, 75)

    def test_distance_breaks_otherwise_close_ties(self) -> None:
        harris_property("TARGET0000002", building={})
        harris_property("CAND00000003", lat_offset="0.0043", building={})
        harris_property("CAND00000004", lat_offset="0.1230", building={})

        scores = _scores("TARGET0000002")
        near_score = scores["CAND00000003"]["similarity_score"]
        far_score = scores["CAND00000004"]["similarity_score"]

        self.assertGreater(near_score, far_score)
        self.assertGreater(near_score - far_score, 2)

    def test_secondary_attributes_separate_near_ties(self) -> None:
        harris_property("TARGET0000003", building={})
        harris_property("CAND00000005", lat_offset="0.0174", building={})
        harris_property(
            "CAND00000006",
            lat_offset="-0.0174",
            building={
                "condition_code": "E",
                "stories": Decimal("1.0"),
                "building_style": "RN",
                "building_class": "R3",
            },
        )

        scores = _scores("TARGET0000003")
        aligned_score = scores["CAND00000005"]["similarity_score"]
        weaker_score = scores["CAND00000006"]["similarity_score"]

        self.assertGreater(aligned_score, weaker_score)
        self.assertGreater(aligned_score - weaker_score, 8)

    def test_near_but_not_identical_match_does_not_cluster_at_97(self) -> None:
        harris_property("TARGET0000007", building={})
        harris_property(
            "CAND00000007",
            lat_offset="0.0217",
            land_area=Decimal("9700"),
            building={
                "heat_area": Decimal("2320"),
                "bedrooms": 5,
                "bathrooms": Decimal("3.0"),
                "effective_year": 2012,
            },
        )

        score = _scores("TARGET0000007")["CAND00000007"]["similarity_score"]

        self.assertGreaterEqual(score, 84)
        self.assertLess(score, 95)

    def test_score_breakdown_explains_component_scores(self) -> None:
        harris_property("TARGET0000008", building={})
        harris_property(
            "CAND00000008",
            lat_offset="0.029",
            building={"bedrooms": 3, "bathrooms": Decimal("3.0")},
        )

        breakdown = _scores("TARGET0000008")["CAND00000008"]["score_breakdown"]

        by_name = {component["name"]: component for component in breakdown}
        self.assertIn("living_area", by_name)
        # One bedroom apart sits on the bedrooms curve's (1.0, 0.62) point.
        self.assertEqual(by_name["bedrooms"]["label"], "Bedrooms")
        self.assertEqual(by_name["bedrooms"]["similarity"], 0.62)
        self.assertEqual(by_name["bedrooms"]["points"], 8.7)

    def test_residential_components_are_skipped_unless_both_sides_have_a_building(self) -> None:
        # No heat area on the subject, so the living-area window does not drop the
        # building-free candidate before scoring.
        harris_property("TARGET0000009", building={"heat_area": None})
        harris_property("CAND00000009", lat_offset="0.01", building=None)

        breakdown = _scores("TARGET0000009")["CAND00000009"]["score_breakdown"]

        self.assertEqual(
            [(c["name"], c["weight"]) for c in breakdown],
            [("land_size", 10.0), ("features", 4.0), ("distance", 4.0)],
        )


class NearbyPropertiesCapTests(TestCase):
    """The shared nearest-properties query caps Harris candidates nearest-first."""

    def _property(self, account_number: str, lat_offset: str, **overrides) -> None:
        heat_area = overrides.pop("heat_area", Decimal("2200"))
        fields = {"is_residential": True, "is_data_ready": True, **overrides}
        record = PropertyRecord.objects.create(
            address=f"{account_number} Cap St",
            city="Houston",
            zipcode="77040",
            owner_name=f"Owner {account_number}",
            account_number=account_number,
            street_number=account_number[-3:],
            street_name="Cap St",
            assessed_value=Decimal("350000"),
            building_area=Decimal("2200"),
            land_area=Decimal("9000"),
            latitude=Decimal("29.8000000") + Decimal(lat_offset),
            longitude=Decimal("-95.5000000"),
            **fields,
        )
        BuildingDetail.objects.create(
            property=record,
            account_number=account_number,
            building_number=1,
            heat_area=heat_area,
            is_active=True,
        )

    def setUp(self):
        self._property("HCAP00000000", "0")
        # Nearest of all, but removed by Harris's own pre-filters before the cap.
        self._property("HCAP00000001", "0.001", heat_area=Decimal("5000"))
        self._property("HCAP00000002", "0.002", is_data_ready=False)
        self._property("HCAP00000003", "0.003")
        self._property("HCAP00000004", "0.004")
        self._property("HCAP00000005", "0.005")

    def _found(self) -> list[str]:
        results = find_similar_properties("HCAP00000000", max_results=50, min_score=0.0)
        return sorted(result["property"].account_number for result in results)

    def test_search_scores_only_the_nearest_capped_candidates_after_pre_filters(self):
        with mock.patch("counties.common.similarity_math.NEARBY_PROPERTIES_CAP", 2):
            self.assertEqual(self._found(), ["HCAP00000003", "HCAP00000004"])

    def test_default_cap_admits_every_nearby_candidate(self):
        self.assertEqual(self._found(), ["HCAP00000003", "HCAP00000004", "HCAP00000005"])
