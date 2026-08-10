"""Site-wide views: the pages that belong to no single county."""

from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse


class AboutPageTests(TestCase):
    def test_about_renders(self):
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)


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
