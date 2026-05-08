import csv
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse

from data.models import AssessmentHistory, BuildingDetail, ExtraFeature, PropertyRecord


class PropertySearchViewTests(TestCase):
    def setUp(self):
        PropertyRecord.objects.create(
            address="123 Main St",
            city="Houston",
            zipcode="77001",
            owner_name="Alice Anderson",
            account_number="0001",
            street_number="123",
            street_name="Main St",
            value=150000,
        )
        PropertyRecord.objects.create(
            address="456 Oak St",
            city="Houston",
            zipcode="77001",
            owner_name="Bob Brown",
            account_number="0002",
            street_number="456",
            street_name="Oak St",
            value=125000,
        )

    def test_index_filters_and_sorts(self):
        response = self.client.get(
            reverse("index"),
            {"zip_code": "77001", "sort": "owner_name", "dir": "desc"},
        )
        self.assertEqual(response.status_code, 200)
        results = response.context["results"]
        self.assertTrue(results)
        self.assertEqual(results[0]["owner_name"], "Bob Brown")

    def test_index_bulk_loads_related_buildings_and_features(self):
        for i in range(5):
            prop = PropertyRecord.objects.create(
                address=f"{i} Search St",
                city="Houston",
                zipcode="77333",
                owner_name=f"Search Owner {i}",
                account_number=f"SEARCH{i:04d}",
                street_number=str(i),
                street_name="Search St",
                value=100000 + i,
            )
            BuildingDetail.objects.create(
                property=prop,
                account_number=prop.account_number,
                building_number=1,
                bedrooms=3,
                bathrooms=2,
                quality_code="C",
                is_active=True,
            )
            ExtraFeature.objects.create(
                property=prop,
                account_number=prop.account_number,
                feature_number=1,
                feature_code="POOL",
                feature_description="Pool",
                is_active=True,
            )

        with self.assertNumQueries(4):
            response = self.client.get(reverse("index"), {"zip_code": "77333"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["results"]), 5)

    def test_export_csv_returns_rows(self):
        response = self.client.get(reverse("export_csv"), {"zip_code": "77001"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode().splitlines()
        self.assertGreaterEqual(len(content), 3)  # header + rows
        self.assertIn("Account Number", content[0])

    def test_export_csv_requires_meaningful_filter(self):
        response = self.client.get(reverse("export_csv"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("meaningful search criteria", response.content.decode())

    def test_export_csv_rejects_short_text_filters(self):
        response = self.client.get(reverse("export_csv"), {"last_name": "Al"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("meaningful search criteria", response.content.decode())

    def test_export_csv_limits_exported_rows(self):
        for i in range(1005):
            PropertyRecord.objects.create(
                address=f"{i} Cap St",
                city="Houston",
                zipcode="77099",
                owner_name=f"Owner {i}",
                account_number=f"CAP{i:04d}",
                street_number=str(i),
                street_name="Cap St",
                value=100000 + i,
            )

        response = self.client.get(reverse("export_csv"), {"zip_code": "77099"})

        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode())))
        self.assertEqual(len(rows), 1001)  # header + 1000 capped data rows

    def test_export_csv_bulk_loads_related_data(self):
        for i in range(5):
            prop = PropertyRecord.objects.create(
                address=f"{i} Bulk St",
                city="Houston",
                zipcode="77111",
                owner_name=f"Bulk Owner {i}",
                account_number=f"BULK{i:04d}",
                street_number=str(i),
                street_name="Bulk St",
                value=100000 + i,
            )
            BuildingDetail.objects.create(
                property=prop,
                account_number=prop.account_number,
                building_number=1,
                bedrooms=3,
                bathrooms=2,
                quality_code="C",
                is_active=True,
            )
            ExtraFeature.objects.create(
                property=prop,
                account_number=prop.account_number,
                feature_number=1,
                feature_code="POOL",
                feature_description="Pool",
                is_active=True,
            )

        with self.assertNumQueries(3):
            response = self.client.get(reverse("export_csv"), {"zip_code": "77111"})

        self.assertEqual(response.status_code, 200)

    def test_export_csv_escapes_formula_like_text_fields(self):
        PropertyRecord.objects.create(
            address="789 Formula St",
            city="Houston",
            zipcode="77222",
            owner_name="=2+2",
            account_number="FORMULA1",
            street_number="@789",
            street_name="+Formula St",
            value=100000,
        )

        response = self.client.get(reverse("export_csv"), {"zip_code": "77222"})

        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode())))
        self.assertEqual(rows[1][1], "'=2+2")
        self.assertEqual(rows[1][2], "'@789")
        self.assertEqual(rows[1][3], "'+Formula St")


class HealthEndpointsTests(TestCase):
    def test_healthz_ok(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    @patch("taxprotest.views.redis")
    def test_readiness_ok_with_redis(self, mock_redis):
        client = MagicMock()
        mock_redis.from_url.return_value = client

        response = self.client.get(reverse("readiness"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redis"], "ok")
        mock_redis.from_url.assert_called_once()
        client.ping.assert_called_once()
        client.close.assert_called_once()

    @patch("taxprotest.views.redis")
    def test_readiness_handles_redis_error(self, mock_redis):
        mock_redis.from_url.side_effect = ConnectionError("boom")

        response = self.client.get(reverse("readiness"))

        self.assertEqual(response.status_code, 503)
        data = response.json()
        self.assertEqual(data["redis"], "error")
        self.assertIn("boom", data["detail_redis"])


class SimilarPropertiesViewTests(TestCase):
    def setUp(self):
        self.target = PropertyRecord.objects.create(
            address="16213 Wall St",
            city="Houston",
            zipcode="77040",
            owner_name="Target Owner",
            account_number="TGT123",
            street_number="16213",
            street_name="Wall St",
            assessed_value=400000,
            building_area=2000,
            latitude=29.8,
            longitude=-95.5,
        )
        BuildingDetail.objects.create(
            property=self.target,
            account_number=self.target.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=4,
            bathrooms=2,
            is_active=True,
        )
        AssessmentHistory.objects.create(
            account_number=self.target.account_number,
            tax_year=2026,
            assessed_value=380000,
        )
        AssessmentHistory.objects.create(
            account_number=self.target.account_number,
            tax_year=2025,
            assessed_value=360000,
        )

        self.low_ppsf_property = PropertyRecord.objects.create(
            address="123 Value Ln",
            city="Houston",
            zipcode="77040",
            owner_name="Low PPSF",
            account_number="LOW001",
            street_number="123",
            street_name="Value Ln",
            assessed_value=300000,
            building_area=2000,
        )
        self.low_ppsf_building = BuildingDetail.objects.create(
            property=self.low_ppsf_property,
            account_number=self.low_ppsf_property.account_number,
            building_number=1,
            heat_area=2000,
            is_active=True,
        )

        self.high_ppsf_property = PropertyRecord.objects.create(
            address="456 Premium Dr",
            city="Houston",
            zipcode="77040",
            owner_name="High PPSF",
            account_number="HIGH001",
            street_number="456",
            street_name="Premium Dr",
            assessed_value=600000,
            building_area=2000,
        )
        self.high_ppsf_building = BuildingDetail.objects.create(
            property=self.high_ppsf_property,
            account_number=self.high_ppsf_property.account_number,
            building_number=1,
            heat_area=2000,
            is_active=True,
        )

    @patch("taxprotest.views.find_similar_properties")
    def test_similar_results_sorted_by_similarity_score(self, mock_find_similar):
        mock_find_similar.return_value = [
            {
                "property": self.low_ppsf_property,
                "building": self.low_ppsf_building,
                "features": [],
                "distance": 0.5,
                "similarity_score": 80,
            },
            {
                "property": self.high_ppsf_property,
                "building": self.high_ppsf_building,
                "features": [],
                "distance": 0.9,
                "similarity_score": 85,
            },
        ]

        response = self.client.get(reverse("similar_properties", args=[self.target.account_number]))

        self.assertEqual(response.status_code, 200)
        results = response.context["results"]
        self.assertTrue(results[0]["is_target"])
        self.assertEqual(results[1]["account_number"], self.high_ppsf_property.account_number)
        self.assertEqual(results[1]["match_label"], "Best match")
        self.assertEqual(results[2]["account_number"], self.low_ppsf_property.account_number)

    @patch("taxprotest.views.find_similar_properties")
    def test_similar_properties_invalid_query_params_use_defaults(self, mock_find_similar):
        mock_find_similar.return_value = []

        response = self.client.get(
            reverse("similar_properties", args=[self.target.account_number]),
            {"max_distance": "not-a-number", "max_results": "many", "min_score": "low"},
        )

        self.assertEqual(response.status_code, 200)
        mock_find_similar.assert_called_once_with(
            account_number=self.target.account_number,
            max_distance_miles=10.0,
            max_results=20,
            min_score=30.0,
        )
        self.assertEqual(response.context["max_distance"], 10.0)
        self.assertEqual(response.context["max_results"], 20)
        self.assertEqual(response.context["min_score"], 30.0)

    @patch("taxprotest.views.find_similar_properties")
    def test_similar_properties_extreme_query_params_are_clamped(self, mock_find_similar):
        mock_find_similar.return_value = []

        response = self.client.get(
            reverse("similar_properties", args=[self.target.account_number]),
            {"max_distance": "9999", "max_results": "9999", "min_score": "-50"},
        )

        self.assertEqual(response.status_code, 200)
        mock_find_similar.assert_called_once_with(
            account_number=self.target.account_number,
            max_distance_miles=50.0,
            max_results=100,
            min_score=0.0,
        )
        self.assertEqual(response.context["max_distance"], 50.0)
        self.assertEqual(response.context["max_results"], 100)
        self.assertEqual(response.context["min_score"], 0.0)

    @patch("taxprotest.views.find_similar_properties")
    def test_similar_properties_includes_assessment_history(self, mock_find_similar):
        mock_find_similar.return_value = []

        response = self.client.get(reverse("similar_properties", args=[self.target.account_number]))

        self.assertEqual(response.status_code, 200)
        history = response.context["assessment_history"]
        self.assertEqual([row["tax_year"] for row in history], [2026, 2025])
        self.assertContains(response, "Five-Year Assessment History")
        self.assertContains(response, "Assessed Value Trend")
        self.assertContains(response, "$380,000")
        self.assertIsNotNone(response.context["assessment_history_chart"])

    @patch("taxprotest.views.find_similar_properties")
    def test_similar_properties_hides_history_when_absent(self, mock_find_similar):
        AssessmentHistory.objects.all().delete()
        mock_find_similar.return_value = []

        response = self.client.get(reverse("similar_properties", args=[self.target.account_number]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["assessment_history"], [])
        self.assertIsNone(response.context["assessment_history_chart"])
        self.assertNotContains(response, "Five-Year Assessment History")


class ProtestRecommendationTests(TestCase):
    """Tests for PPSF-based protest recommendation logic."""

    def setUp(self):
        # Target property: $150/sqft (high PPSF)
        self.target_high = PropertyRecord.objects.create(
            address="100 Target St",
            city="Houston",
            zipcode="77001",
            owner_name="Target Owner High",
            account_number="TARGET001",
            street_number="100",
            street_name="Target St",
            assessed_value=300000,
            latitude=29.760,
            longitude=-95.370,
        )
        BuildingDetail.objects.create(
            property=self.target_high,
            account_number="TARGET001",
            building_number=1,
            heat_area=2000,
            is_active=True,
        )

        # Target property: $120/sqft (near median)
        self.target_neutral = PropertyRecord.objects.create(
            address="200 Target St",
            city="Houston",
            zipcode="77001",
            owner_name="Target Owner Neutral",
            account_number="TARGET002",
            street_number="200",
            street_name="Target St",
            assessed_value=240000,
            latitude=29.761,
            longitude=-95.371,
        )
        BuildingDetail.objects.create(
            property=self.target_neutral,
            account_number="TARGET002",
            building_number=1,
            heat_area=2000,
            is_active=True,
        )

        # Comparable properties with various PPSF values
        comps_data = [
            ("COMP001", 100, 1800, 180000),  # $100/sqft
            ("COMP002", 101, 1900, 209000),  # $110/sqft
            ("COMP003", 102, 2000, 240000),  # $120/sqft
            ("COMP004", 103, 2100, 262500),  # $125/sqft
            ("COMP005", 104, 2200, 286000),  # $130/sqft
        ]

        for acct, street_num, area, value in comps_data:
            prop = PropertyRecord.objects.create(
                address=f"{street_num} Comp St",
                city="Houston",
                zipcode="77001",
                owner_name=f"Owner {acct}",
                account_number=acct,
                street_number=str(street_num),
                street_name="Comp St",
                assessed_value=value,
                latitude=29.760 + (street_num - 100) * 0.001,
                longitude=-95.370,
            )
            BuildingDetail.objects.create(
                property=prop,
                account_number=acct,
                building_number=1,
                heat_area=area,
                is_active=True,
            )

    @patch("taxprotest.views.find_similar_properties")
    def test_strong_protest_recommendation(self, mock_find_similar):
        """Test that high PPSF generates 'strong' recommendation."""
        # Mock comparables with lower PPSF
        mock_find_similar.return_value = [
            {
                "property": PropertyRecord.objects.get(account_number="COMP001"),
                "building": BuildingDetail.objects.get(account_number="COMP001"),
                "features": [],
                "distance": 0.5,
                "similarity_score": 75,
            },
            {
                "property": PropertyRecord.objects.get(account_number="COMP002"),
                "building": BuildingDetail.objects.get(account_number="COMP002"),
                "features": [],
                "distance": 0.6,
                "similarity_score": 70,
            },
            {
                "property": PropertyRecord.objects.get(account_number="COMP003"),
                "building": BuildingDetail.objects.get(account_number="COMP003"),
                "features": [],
                "distance": 0.7,
                "similarity_score": 68,
            },
        ]

        response = self.client.get(reverse("similar_properties", args=["TARGET001"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["protest_recommendation_level"], "strong")
        self.assertIn("Recommend protesting", response.context["protest_recommendation"])
        self.assertIsNotNone(response.context["ppsf_median"])
        self.assertEqual(response.context["comparable_count"], 3)

    @patch("taxprotest.views.find_similar_properties")
    def test_neutral_recommendation(self, mock_find_similar):
        """Test that PPSF close to median generates 'neutral' recommendation."""
        mock_find_similar.return_value = [
            {
                "property": PropertyRecord.objects.get(account_number="COMP002"),
                "building": BuildingDetail.objects.get(account_number="COMP002"),
                "features": [],
                "distance": 0.5,
                "similarity_score": 75,
            },
            {
                "property": PropertyRecord.objects.get(account_number="COMP003"),
                "building": BuildingDetail.objects.get(account_number="COMP003"),
                "features": [],
                "distance": 0.6,
                "similarity_score": 72,
            },
            {
                "property": PropertyRecord.objects.get(account_number="COMP004"),
                "building": BuildingDetail.objects.get(account_number="COMP004"),
                "features": [],
                "distance": 0.7,
                "similarity_score": 70,
            },
        ]

        response = self.client.get(reverse("similar_properties", args=["TARGET002"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["protest_recommendation_level"], "neutral")
        self.assertIn("Borderline", response.context["protest_recommendation"])

    @patch("taxprotest.views.find_similar_properties")
    def test_insufficient_data_no_recommendation(self, mock_find_similar):
        """Test that fewer than 3 comparables shows no recommendation."""
        mock_find_similar.return_value = [
            {
                "property": PropertyRecord.objects.get(account_number="COMP001"),
                "building": BuildingDetail.objects.get(account_number="COMP001"),
                "features": [],
                "distance": 0.5,
                "similarity_score": 75,
            },
            {
                "property": PropertyRecord.objects.get(account_number="COMP002"),
                "building": BuildingDetail.objects.get(account_number="COMP002"),
                "features": [],
                "distance": 0.6,
                "similarity_score": 70,
            },
        ]

        response = self.client.get(reverse("similar_properties", args=["TARGET001"]))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["protest_recommendation"])
        self.assertEqual(response.context["comparable_count"], 0)

    @patch("taxprotest.views.find_similar_properties")
    def test_recommendation_with_missing_ppsf(self, mock_find_similar):
        """Test that comparables without PPSF are excluded from calculation."""
        # Create a comp without assessed value
        prop_no_value = PropertyRecord.objects.create(
            address="999 No Value St",
            city="Houston",
            zipcode="77001",
            owner_name="No Value Owner",
            account_number="COMPNOVAL",
            street_number="999",
            street_name="No Value St",
            assessed_value=None,  # Missing value
            latitude=29.765,
            longitude=-95.370,
        )
        BuildingDetail.objects.create(
            property=prop_no_value,
            account_number="COMPNOVAL",
            building_number=1,
            heat_area=2000,
            is_active=True,
        )

        mock_find_similar.return_value = [
            {
                "property": PropertyRecord.objects.get(account_number="COMP001"),
                "building": BuildingDetail.objects.get(account_number="COMP001"),
                "features": [],
                "distance": 0.5,
                "similarity_score": 75,
            },
            {
                "property": PropertyRecord.objects.get(account_number="COMP002"),
                "building": BuildingDetail.objects.get(account_number="COMP002"),
                "features": [],
                "distance": 0.6,
                "similarity_score": 70,
            },
            {
                "property": PropertyRecord.objects.get(account_number="COMP003"),
                "building": BuildingDetail.objects.get(account_number="COMP003"),
                "features": [],
                "distance": 0.7,
                "similarity_score": 68,
            },
            {
                "property": prop_no_value,
                "building": BuildingDetail.objects.get(account_number="COMPNOVAL"),
                "features": [],
                "distance": 0.8,
                "similarity_score": 65,
            },
        ]

        response = self.client.get(reverse("similar_properties", args=["TARGET001"]))

        self.assertEqual(response.status_code, 200)
        # Should only count 3 properties with valid PPSF, excluding the one without assessed value
        self.assertEqual(response.context["comparable_count"], 3)
        self.assertIsNotNone(response.context["protest_recommendation"])


class ProtestAnalysisViewTests(TestCase):
    def setUp(self):
        self.target = PropertyRecord.objects.create(
            address="16213 Wall St",
            city="Houston",
            zipcode="77040",
            owner_name="Target Owner",
            account_number="PROTEST_TGT",
            street_number="16213",
            street_name="Wall St",
            assessed_value=370000,
            building_area=2000,
            latitude=29.8,
            longitude=-95.5,
        )
        self.target_building = BuildingDetail.objects.create(
            property=self.target,
            account_number=self.target.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=4,
            bathrooms=2,
            quality_code="B",
            year_built=2005,
            is_active=True,
        )
        AssessmentHistory.objects.create(
            account_number=self.target.account_number,
            tax_year=2026,
            assessed_value=355000,
            appraised_value=355000,
            market_value=390000,
            prior_appraised_value=340000,
            prior_market_value=360000,
            new_construction_value=0,
            cap_account="Y",
        )
        AssessmentHistory.objects.create(
            account_number=self.target.account_number,
            tax_year=2025,
            assessed_value=340000,
            appraised_value=340000,
            market_value=360000,
        )
        self.comp = PropertyRecord.objects.create(
            address="100 Similar Ln",
            city="Houston",
            zipcode="77040",
            owner_name="Comp Owner",
            account_number="PROTEST_CMP",
            street_number="100",
            street_name="Similar Ln",
            assessed_value=320000,
            building_area=2000,
            latitude=29.81,
            longitude=-95.5,
        )
        self.comp_building = BuildingDetail.objects.create(
            property=self.comp,
            account_number=self.comp.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=4,
            bathrooms=2,
            quality_code="B",
            year_built=2004,
            is_active=True,
        )

    def _similar_result(self, prop, building, score=75.0, distance=0.5):
        return {
            "property": prop,
            "building": building,
            "features": [],
            "distance": distance,
            "similarity_score": score,
            "score_breakdown": [
                {
                    "name": "living_area",
                    "label": "Living Area",
                    "similarity": 1.0,
                    "points": 24.0,
                    "weight": 24.0,
                }
            ],
        }

    def test_404_for_unknown_account(self):
        response = self.client.get(reverse("protest_analysis", args=["DOESNOTEXIST"]))
        self.assertEqual(response.status_code, 404)

    @patch("taxprotest.views.find_similar_properties")
    def test_200_and_required_context_keys_present(self, mock_find):
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building)]
        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))
        self.assertEqual(response.status_code, 200)
        ctx = response.context
        for key in [
            "target_property",
            "target_building",
            "subject_value_per_sqft",
            "comps",
            "median_comp_value_per_sqft",
            "equity_gap_per_sqft",
            "estimated_savings",
            "comps_below_subject",
            "qualifying_comp_count",
            "min_score",
            "pdf_export_url",
        ]:
            self.assertIn(key, ctx, f"Missing context key: {key}")

    @patch("taxprotest.views.find_similar_properties")
    def test_equity_gap_and_savings_computed_correctly(self, mock_find):
        # target: $370,000 / 2,000 sqft = $185/sqft
        # comp:   $320,000 / 2,000 sqft = $160/sqft
        # gap: 185 - 160 = $25/sqft  |  savings: 25 * 2000 = $50,000
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building)]
        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))
        ctx = response.context
        self.assertAlmostEqual(ctx["subject_value_per_sqft"], 185.0, places=1)
        self.assertAlmostEqual(ctx["median_comp_value_per_sqft"], 160.0, places=1)
        self.assertAlmostEqual(ctx["equity_gap_per_sqft"], 25.0, places=1)
        self.assertAlmostEqual(ctx["estimated_savings"], 50000.0, places=0)

    @patch("taxprotest.views.find_similar_properties")
    def test_min_score_clamped_to_52_when_below(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis", args=[self.target.account_number]),
            {"min_score": "10"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["min_score"], 52.0)
        mock_find.assert_called_with(
            account_number=self.target.account_number,
            max_distance_miles=10.0,
            max_results=50,
            min_score=52.0,
        )

    @patch("taxprotest.views.find_similar_properties")
    def test_min_score_defaults_to_70_when_not_provided(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))
        self.assertEqual(response.context["min_score"], 70.0)
        mock_find.assert_called_with(
            account_number=self.target.account_number,
            max_distance_miles=10.0,
            max_results=50,
            min_score=70.0,
        )

    @patch("taxprotest.views.find_similar_properties")
    def test_no_equity_summary_when_subject_missing_assessed_value(self, mock_find):
        self.target.assessed_value = None
        self.target.save()
        mock_find.return_value = []
        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["subject_value_per_sqft"])
        self.assertIsNone(response.context["equity_gap_per_sqft"])
        self.assertIsNone(response.context["estimated_savings"])
        self.assertIsNone(response.context["median_comp_value_per_sqft"])

    @patch("taxprotest.views.find_similar_properties")
    def test_comp_delta_is_negative_when_comp_cheaper_than_subject(self, mock_find):
        # subject: $185/sqft, comp: $160/sqft → delta = -25 (comp is cheaper)
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building)]
        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))
        comps = response.context["comps"]
        self.assertEqual(len(comps), 1)
        self.assertIn("comp_delta", comps[0])
        self.assertAlmostEqual(comps[0]["comp_delta"], -25.0, places=1)

    @patch("taxprotest.views.find_similar_properties")
    def test_comps_below_subject_counted_correctly(self, mock_find):
        # 1 comp at $160/sqft < subject $185/sqft → count = 1
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building)]
        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))
        self.assertEqual(response.context["comps_below_subject"], 1)

    @patch("taxprotest.views.find_similar_properties")
    def test_protest_analysis_includes_assessment_history(self, mock_find):
        mock_find.return_value = []

        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))

        self.assertEqual(response.status_code, 200)
        history = response.context["assessment_history"]
        self.assertEqual([row["tax_year"] for row in history], [2026, 2025])
        self.assertEqual(history[0]["increase_percent"], Decimal("4.41"))
        self.assertEqual(history[0]["cap_status"]["status"], "within_limit")
        self.assertContains(response, "Five-Year Assessment History")
        self.assertContains(response, "YoY Change")
        self.assertContains(response, "Cap Status")
        self.assertContains(response, "Assessed Value Trend")
        self.assertIsNotNone(response.context["assessment_history_chart"])

    @patch("taxprotest.views.find_similar_properties")
    def test_protest_analysis_displays_score_breakdown(self, mock_find):
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building, score=87.4)]

        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["comps"][0]["similarity_score"], 87.4)
        self.assertEqual(response.context["comps"][0]["score_breakdown"][0]["label"], "Living Area")
        self.assertContains(response, "87.4")
        self.assertContains(response, "Score Details")

    @patch("taxprotest.views.find_similar_properties")
    def test_protest_analysis_hides_history_when_absent(self, mock_find):
        AssessmentHistory.objects.all().delete()
        mock_find.return_value = []

        response = self.client.get(reverse("protest_analysis", args=[self.target.account_number]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["assessment_history"], [])
        self.assertIsNone(response.context["assessment_history_chart"])
        self.assertNotContains(response, "Five-Year Assessment History")


class ProtestAnalysisExportTests(TestCase):
    def setUp(self):
        self.target = PropertyRecord.objects.create(
            address="200 Export Ave",
            city="Houston",
            zipcode="77040",
            owner_name="Export Owner",
            account_number="EXPORT_TGT",
            street_number="200",
            street_name="Export Ave",
            assessed_value=350000,
            building_area=2000,
            latitude=29.8,
            longitude=-95.5,
        )
        self.target_building = BuildingDetail.objects.create(
            property=self.target,
            account_number=self.target.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=3,
            bathrooms=2,
            quality_code="C",
            year_built=2000,
            is_active=True,
        )
        self.comp = PropertyRecord.objects.create(
            address="201 Export Ave",
            city="Houston",
            zipcode="77040",
            owner_name="Comp Owner",
            account_number="EXPORT_CMP",
            street_number="201",
            street_name="Export Ave",
            assessed_value=300000,
            building_area=2000,
            latitude=29.81,
            longitude=-95.5,
        )
        self.comp_building = BuildingDetail.objects.create(
            property=self.comp,
            account_number=self.comp.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=3,
            bathrooms=2,
            quality_code="C",
            year_built=1999,
            is_active=True,
        )

    def _similar_result(self):
        return {
            "property": self.comp,
            "building": self.comp_building,
            "features": [],
            "distance": 0.5,
            "similarity_score": 76.0,
            "score_breakdown": [
                {
                    "name": "living_area",
                    "label": "Living Area",
                    "similarity": 1.0,
                    "points": 24.0,
                    "weight": 24.0,
                }
            ],
        }

    def test_404_for_unknown_account(self):
        response = self.client.get(reverse("protest_analysis_export", args=["DOESNOTEXIST"]))
        self.assertEqual(response.status_code, 404)

    @patch("taxprotest.views.find_similar_properties")
    def test_returns_csv_content_type(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis_export", args=[self.target.account_number])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])

    @patch("taxprotest.views.find_similar_properties")
    def test_csv_filename_contains_account_number(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis_export", args=[self.target.account_number])
        )
        self.assertIn(self.target.account_number, response["Content-Disposition"])

    @patch("taxprotest.views.find_similar_properties")
    def test_csv_header_row_has_required_columns(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis_export", args=[self.target.account_number])
        )
        content = response.content.decode()
        header = content.splitlines()[0]
        for col in [
            "address",
            "similarity_score",
            "similarity_label",
            "living_area_sqft",
            "bedrooms",
            "bathrooms",
            "year_built",
            "quality_code",
            "condition_code",
            "assessed_value",
            "value_per_sqft",
            "delta_vs_subject_per_sqft",
            "score_breakdown",
        ]:
            self.assertIn(col, header, f"Missing CSV column: {col}")

    @patch("taxprotest.views.find_similar_properties")
    def test_csv_data_row_contains_comp_values(self, mock_find):
        mock_find.return_value = [self._similar_result()]
        response = self.client.get(
            reverse("protest_analysis_export", args=[self.target.account_number])
        )
        content = response.content.decode()
        lines = content.splitlines()
        self.assertEqual(len(lines), 2)  # header + 1 data row
        # delta_vs_subject_per_sqft = comp $/sqft - subject $/sqft
        # comp: 300000/2000 = $150/sqft; subject: 350000/2000 = $175/sqft → -25.00
        self.assertIn("201 Export Ave", lines[1])  # full address field
        self.assertIn("150.00", lines[1])  # value_per_sqft
        self.assertIn("-25.00", lines[1])  # delta_vs_subject_per_sqft
        self.assertIn("Living Area", lines[1])

    @patch("taxprotest.views.find_similar_properties")
    def test_pdf_export_returns_pdf(self, mock_find):
        mock_find.return_value = [self._similar_result()]

        response = self.client.get(
            reverse("protest_analysis_pdf", args=[self.target.account_number])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn(self.target.account_number, response["Content-Disposition"])
