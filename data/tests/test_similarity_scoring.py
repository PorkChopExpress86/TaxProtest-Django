from decimal import Decimal

from django.test import TestCase

from data.models import BuildingDetail, PropertyRecord
from data.similarity import (
    calculate_similarity_details,
    calculate_similarity_score,
    get_similarity_label,
)


class SimilarityScoringTests(TestCase):
    def create_property_with_building(
        self,
        account_number: str,
        *,
        property_overrides: dict | None = None,
        building_overrides: dict | None = None,
    ) -> tuple[PropertyRecord, BuildingDetail]:
        property_defaults = {
            "address": f"{account_number} Test St",
            "city": "Houston",
            "zipcode": "77040",
            "owner_name": f"Owner {account_number}",
            "account_number": account_number,
            "street_number": account_number[-3:],
            "street_name": "Test St",
            "assessed_value": Decimal("350000"),
            "building_area": Decimal("2200"),
            "land_area": Decimal("9000"),
            "latitude": Decimal("29.8000000"),
            "longitude": Decimal("-95.5000000"),
        }
        if property_overrides:
            property_defaults.update(property_overrides)

        property_record = PropertyRecord.objects.create(**property_defaults)

        building_defaults = {
            "property": property_record,
            "account_number": account_number,
            "building_number": 1,
            "building_type": "A1",
            "building_style": "TR",
            "building_class": "R1",
            "quality_code": "A",
            "condition_code": "B",
            "year_built": 2005,
            "effective_year": 2008,
            "heat_area": Decimal("2200"),
            "stories": Decimal("2.0"),
            "bedrooms": 4,
            "bathrooms": Decimal("2.5"),
            "half_baths": 1,
            "is_active": True,
        }
        if building_overrides:
            building_defaults.update(building_overrides)

        building = BuildingDetail.objects.create(**building_defaults)
        return property_record, building

    def test_granular_score_separates_best_from_ok_match(self) -> None:
        target, target_building = self.create_property_with_building("TARGET0000001")
        perfect, perfect_building = self.create_property_with_building("CAND00000001")
        ok_match, ok_match_building = self.create_property_with_building(
            "CAND00000002",
            property_overrides={
                "land_area": Decimal("12000"),
                "assessed_value": Decimal("320000"),
            },
            building_overrides={
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

        perfect_score = calculate_similarity_score(
            target,
            perfect,
            target_building,
            perfect_building,
            distance=0.4,
            max_distance_miles=10.0,
        )
        ok_match_score = calculate_similarity_score(
            target,
            ok_match,
            target_building,
            ok_match_building,
            distance=4.8,
            max_distance_miles=10.0,
        )

        self.assertGreater(perfect_score, 95)
        self.assertGreater(perfect_score, ok_match_score + 25)
        self.assertGreaterEqual(ok_match_score, 35)
        self.assertLess(ok_match_score, 75)

    def test_distance_breaks_otherwise_close_ties(self) -> None:
        target, target_building = self.create_property_with_building("TARGET0000002")
        near_candidate, near_building = self.create_property_with_building("CAND00000003")
        far_candidate, far_building = self.create_property_with_building("CAND00000004")

        near_score = calculate_similarity_score(
            target,
            near_candidate,
            target_building,
            near_building,
            distance=0.3,
            max_distance_miles=10.0,
        )
        far_score = calculate_similarity_score(
            target,
            far_candidate,
            target_building,
            far_building,
            distance=8.5,
            max_distance_miles=10.0,
        )

        self.assertGreater(near_score, far_score)
        self.assertGreater(near_score - far_score, 2)

    def test_secondary_attributes_separate_near_ties(self) -> None:
        target, target_building = self.create_property_with_building("TARGET0000003")
        aligned, aligned_building = self.create_property_with_building("CAND00000005")
        weaker, weaker_building = self.create_property_with_building(
            "CAND00000006",
            building_overrides={
                "condition_code": "E",
                "stories": Decimal("1.0"),
                "building_style": "RN",
                "building_class": "R3",
            },
        )

        aligned_score = calculate_similarity_score(
            target,
            aligned,
            target_building,
            aligned_building,
            distance=1.2,
            max_distance_miles=10.0,
        )
        weaker_score = calculate_similarity_score(
            target,
            weaker,
            target_building,
            weaker_building,
            distance=1.2,
            max_distance_miles=10.0,
        )

        self.assertGreater(aligned_score, weaker_score)
        self.assertGreater(aligned_score - weaker_score, 8)

    def test_near_but_not_identical_match_no_longer_clusters_at_97(self) -> None:
        target, target_building = self.create_property_with_building("TARGET0000007")
        candidate, candidate_building = self.create_property_with_building(
            "CAND00000007",
            property_overrides={"land_area": Decimal("9700")},
            building_overrides={
                "heat_area": Decimal("2320"),
                "bedrooms": 5,
                "bathrooms": Decimal("3.0"),
                "effective_year": 2012,
            },
        )

        score = calculate_similarity_score(
            target,
            candidate,
            target_building,
            candidate_building,
            distance=1.5,
            max_distance_miles=10.0,
        )

        self.assertGreaterEqual(score, 84)
        self.assertLess(score, 95)

    def test_similarity_details_explain_component_scores(self) -> None:
        target, target_building = self.create_property_with_building("TARGET0000008")
        candidate, candidate_building = self.create_property_with_building(
            "CAND00000008",
            building_overrides={"bedrooms": 3, "bathrooms": Decimal("3.0")},
        )

        details = calculate_similarity_details(
            target,
            candidate,
            target_building,
            candidate_building,
            distance=2.0,
            max_distance_miles=10.0,
        )

        self.assertIn("score", details)
        self.assertIn("components", details)
        component_names = {component["name"] for component in details["components"]}
        self.assertIn("living_area", component_names)
        self.assertIn("bedrooms", component_names)
        bedrooms = next(c for c in details["components"] if c["name"] == "bedrooms")
        self.assertEqual(bedrooms["label"], "Bedrooms")
        self.assertLess(bedrooms["similarity"], 1.0)
        self.assertGreater(bedrooms["points"], 0)

    def test_match_labels_cover_all_user_facing_tiers(self) -> None:
        self.assertEqual(get_similarity_label(90), "Best match")
        self.assertEqual(get_similarity_label(72), "Highly similar")
        self.assertEqual(get_similarity_label(58), "Good match")
        self.assertEqual(get_similarity_label(40), "OK match")
        self.assertEqual(get_similarity_label(20), "Broad match")
