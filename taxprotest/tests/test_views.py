from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse

from data.models import BuildingDetail, PropertyRecord


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

    def test_export_csv_returns_rows(self):
        response = self.client.get(reverse("export_csv"), {"zip_code": "77001"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode().splitlines()
        self.assertGreaterEqual(len(content), 3)  # header + rows
        self.assertIn("Account Number", content[0])


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
    def test_similar_results_sorted_by_ppsf(self, mock_find_similar):
        mock_find_similar.return_value = [
            {
                "property": self.high_ppsf_property,
                "building": self.high_ppsf_building,
                "features": [],
                "distance": 0.9,
                "similarity_score": 85,
            },
            {
                "property": self.low_ppsf_property,
                "building": self.low_ppsf_building,
                "features": [],
                "distance": 0.5,
                "similarity_score": 80,
            },
        ]

        response = self.client.get(
            reverse("similar_properties", args=[self.target.account_number])
        )

        self.assertEqual(response.status_code, 200)
        results = response.context["results"]
        self.assertTrue(results[0]["is_target"])
        self.assertLess(results[1]["ppsf"], results[2]["ppsf"])


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

        response = self.client.get(
            reverse("similar_properties", args=["TARGET001"])
        )

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

        response = self.client.get(
            reverse("similar_properties", args=["TARGET002"])
        )

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

        response = self.client.get(
            reverse("similar_properties", args=["TARGET001"])
        )

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

        response = self.client.get(
            reverse("similar_properties", args=["TARGET001"])
        )

        self.assertEqual(response.status_code, 200)
        # Should only count 3 properties with valid PPSF, excluding the one without assessed value
        self.assertEqual(response.context["comparable_count"], 3)
        self.assertIsNotNone(response.context["protest_recommendation"])
