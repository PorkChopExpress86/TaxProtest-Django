from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse

from data.models import PropertyRecord


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