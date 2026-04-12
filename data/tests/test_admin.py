from __future__ import annotations

from typing import cast
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import UserManager
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse


class AdminETLPipelineViewTests(TestCase):
	def setUp(self) -> None:
		user_model = get_user_model()
		user_manager = cast(UserManager, user_model.objects)
		self.superuser = user_manager.create_superuser(
			username="admin",
			email="admin@example.com",
			password="password123",
		)
		self.regular_user = user_manager.create_user(
			username="regular",
			email="regular@example.com",
			password="password123",
		)
		self.changelist_url = reverse("admin:data_downloadrecord_changelist")
		self.etl_url = reverse("admin:data_downloadrecord_etl_pipeline")

	def test_etl_pipeline_page_requires_admin_access(self) -> None:
		response = self.client.get(self.etl_url)

		self.assertEqual(response.status_code, 302)
		self.assertIn("/admin/login/", response["Location"])

	def test_non_staff_user_cannot_access_etl_pipeline_page(self) -> None:
		self.client.force_login(self.regular_user)

		response = self.client.get(self.etl_url)

		self.assertEqual(response.status_code, 302)
		self.assertIn("/admin/login/", response["Location"])

	def test_admin_changelist_shows_etl_pipeline_link(self) -> None:
		self.client.force_login(self.superuser)

		response = self.client.get(self.changelist_url)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, reverse("admin:data_downloadrecord_etl_pipeline"))
		self.assertContains(response, "Re-download &amp; run ETL pipeline")

	def test_superuser_can_view_etl_pipeline_page(self) -> None:
		self.client.force_login(self.superuser)

		response = self.client.get(self.etl_url)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Queue ETL pipeline")
		self.assertContains(response, "HCAD data year")

	@patch("data.admin.run_etl_pipeline.delay")
	def test_superuser_can_queue_etl_pipeline(self, mocked_delay) -> None:
		self.client.force_login(self.superuser)
		mocked_delay.return_value.id = "etl-task-123"

		response = self.client.post(self.etl_url, {"data_year": 2026}, follow=True)

		mocked_delay.assert_called_once_with(
			skip_download=False,
			skip_extract=False,
			skip_load=False,
			data_year=2026,
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.request["PATH_INFO"], self.changelist_url)

		queued_messages = [message.message for message in get_messages(response.wsgi_request)]
		self.assertTrue(any("etl-task-123" in message for message in queued_messages))
