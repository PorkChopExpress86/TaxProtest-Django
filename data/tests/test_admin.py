from __future__ import annotations

from decimal import Decimal
from typing import cast
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import UserManager
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from data.models import (
    AssessmentHistory,
    DownloadRecord,
    PropertyJurisdictionExemption,
    PropertyRecord,
    TaxUnitRate,
)


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
        self.staff_user = user_manager.create_user(
            username="staff",
            email="staff@example.com",
            password="password123",
            is_staff=True,
        )
        self.changelist_url = reverse("admin:data_downloadrecord_changelist")
        self.etl_url = reverse("admin:data_downloadrecord_etl_pipeline")

        lock_patcher = patch("data.admin.pipeline_lock.current_run")
        self.mock_current_run = lock_patcher.start()
        self.mock_current_run.return_value = None
        self.addCleanup(lock_patcher.stop)

    def test_etl_pipeline_page_requires_admin_access(self) -> None:
        response = self.client.get(self.etl_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_non_staff_user_cannot_access_etl_pipeline_page(self) -> None:
        self.client.force_login(self.regular_user)

        response = self.client.get(self.etl_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_staff_non_superuser_forbidden_from_etl_pipeline_page_get(self) -> None:
        self.client.force_login(self.staff_user)

        response = self.client.get(self.etl_url)

        self.assertEqual(response.status_code, 403)

    def test_staff_non_superuser_forbidden_from_etl_pipeline_page_post(self) -> None:
        self.client.force_login(self.staff_user)

        response = self.client.post(self.etl_url, {"data_year": 2026})

        self.assertEqual(response.status_code, 403)

    def test_staff_non_superuser_forbidden_from_triggering_gis_import(self) -> None:
        self.client.force_login(self.staff_user)

        response = self.client.post(reverse("admin:data_downloadrecord_trigger_gis_import"))

        self.assertEqual(response.status_code, 403)

    def test_staff_non_superuser_forbidden_from_triggering_building_import(self) -> None:
        self.client.force_login(self.staff_user)

        response = self.client.post(reverse("admin:data_downloadrecord_trigger_building_import"))

        self.assertEqual(response.status_code, 403)

    def test_staff_non_superuser_forbidden_from_task_status(self) -> None:
        self.client.force_login(self.staff_user)

        response = self.client.get(
            reverse("admin:data_downloadrecord_task_status", args=["some-task-id"])
        )

        self.assertEqual(response.status_code, 403)

    def test_admin_changelist_shows_etl_pipeline_link(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(self.changelist_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin:data_downloadrecord_etl_pipeline"))
        self.assertContains(response, "Re-download &amp; run ETL pipeline")

    def test_staff_non_superuser_with_view_permission_does_not_see_etl_pipeline_link(
        self,
    ) -> None:
        from django.contrib.auth.models import Permission

        self.staff_user.user_permissions.add(
            Permission.objects.get(codename="view_downloadrecord", content_type__app_label="data")
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(self.changelist_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Re-download &amp; run ETL pipeline")
        self.assertNotContains(response, "Trigger GIS import")
        self.assertNotContains(response, "Trigger building import")

    def test_staff_non_superuser_does_not_see_etl_operations_module_on_index(self) -> None:
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ETL Pipeline Operations")

    def test_superuser_sees_etl_operations_module_on_index(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ETL Pipeline Operations")

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

    @patch("data.admin.run_etl_pipeline.delay")
    def test_queue_is_rejected_when_a_pipeline_is_already_running(self, mocked_delay) -> None:
        self.mock_current_run.return_value = {
            "scope": "gis-only",
            "task_id": "in-flight",
            "started_at": 0,
        }
        self.client.force_login(self.superuser)

        response = self.client.post(self.etl_url, {"data_year": 2026}, follow=True)

        mocked_delay.assert_not_called()
        self.assertEqual(response.request["PATH_INFO"], self.changelist_url)
        queued_messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("gis-only" in message for message in queued_messages))

    @patch("data.admin.download_and_import_gis_data.delay")
    def test_trigger_gis_import_is_rejected_when_a_pipeline_is_already_running(
        self, mocked_delay
    ) -> None:
        self.mock_current_run.return_value = {
            "scope": "full",
            "task_id": "in-flight",
            "started_at": 0,
        }
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("admin:data_downloadrecord_trigger_gis_import"), follow=True
        )

        mocked_delay.assert_not_called()
        self.assertEqual(response.request["PATH_INFO"], self.changelist_url)
        queued_messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("already in progress" in message for message in queued_messages))

    def test_etl_pipeline_page_shows_running_banner(self) -> None:
        self.mock_current_run.return_value = {
            "scope": "building-only",
            "task_id": "x",
            "started_at": 0,
        }
        self.client.force_login(self.superuser)

        response = self.client.get(self.etl_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "currently in progress")
        self.assertContains(response, "building-only")

    def test_status_panel_shows_initial_badge_markup(self) -> None:
        self.client.force_login(self.superuser)
        session = self.client.session
        session["etl_last_task_id"] = "etl-task-456"
        session["etl_last_task_type"] = "Full ETL Pipeline"
        session.save()

        response = self.client.get(self.etl_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="etl-task-state" class="etl-badge etl-badge-unknown"')

    @patch("celery.result.AsyncResult")
    def test_task_status_success_includes_result_payload(self, mocked_async_result) -> None:
        mocked_async_result.return_value.state = "SUCCESS"
        mocked_async_result.return_value.info = None
        mocked_async_result.return_value.result = {"rows_loaded": 42}
        self.client.force_login(self.superuser)

        response = self.client.get(
            reverse("admin:data_downloadrecord_task_status", args=["fake-task-id"])
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["state"], "SUCCESS")
        self.assertEqual(data["result"], {"rows_loaded": 42})

    @patch("celery.result.AsyncResult")
    def test_task_status_failure_includes_error_message(self, mocked_async_result) -> None:
        mocked_async_result.return_value.state = "FAILURE"
        mocked_async_result.return_value.info = RuntimeError("boom")
        mocked_async_result.return_value.result = None
        self.client.force_login(self.superuser)

        response = self.client.get(
            reverse("admin:data_downloadrecord_task_status", args=["fake-task-id"])
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["state"], "FAILURE")
        self.assertEqual(data["error"], "boom")


class AdminBrandingTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        user_model = get_user_model()
        user_manager = cast(UserManager, user_model.objects)
        self.superuser = user_manager.create_superuser(
            username="branding-admin",
            email="branding-admin@example.com",
            password="password123",
        )

    def test_site_is_branded(self) -> None:
        self.assertEqual(admin.site.site_header, "Home Values Admin")
        self.assertEqual(admin.site.site_title, "Home Values Admin")
        self.assertEqual(admin.site.index_title, "ETL & Data Administration")

    def test_index_page_renders_branding_and_custom_stylesheet(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Home Values Admin")
        self.assertContains(response, "admin/css/custom_admin.css")


class AssessmentHistoryAdminTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        user_manager = cast(UserManager, user_model.objects)
        self.superuser = user_manager.create_superuser(
            username="history-admin",
            email="history-admin@example.com",
            password="password123",
        )
        self.history = AssessmentHistory.objects.create(
            account_number="1234567890123",
            tax_year=2026,
            assessed_value=Decimal("250000.00"),
        )

    def test_registered_as_read_only(self) -> None:
        model_admin = admin.site._registry[AssessmentHistory]

        self.assertFalse(model_admin.has_add_permission(request=None))
        self.assertFalse(model_admin.has_change_permission(request=None))
        self.assertFalse(model_admin.has_delete_permission(request=None))

    def test_changelist_renders(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:data_assessmenthistory_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1234567890123")

    def test_add_and_delete_blocked_over_http(self) -> None:
        self.client.force_login(self.superuser)

        add_response = self.client.get(reverse("admin:data_assessmenthistory_add"))
        delete_response = self.client.get(
            reverse("admin:data_assessmenthistory_delete", args=[self.history.pk])
        )

        self.assertEqual(add_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)

    def test_change_is_read_only_over_http(self) -> None:
        self.client.force_login(self.superuser)
        change_url = reverse("admin:data_assessmenthistory_change", args=[self.history.pk])

        get_response = self.client.get(change_url)
        post_response = self.client.post(change_url, {"assessed_value": "1.00"})

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 403)
        self.history.refresh_from_db()
        self.assertEqual(self.history.assessed_value, Decimal("250000.00"))


class PropertyRecordAdminTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        user_manager = cast(UserManager, user_model.objects)
        self.superuser = user_manager.create_superuser(
            username="property-admin",
            email="property-admin@example.com",
            password="password123",
        )
        PropertyRecord.objects.create(
            address="123 Main St",
            city="Houston",
            zipcode="77002",
            account_number="9876543210123",
            is_residential=True,
            is_data_ready=True,
        )
        PropertyRecord.objects.create(
            address="456 Oak St",
            city="Houston",
            zipcode="77003",
            account_number="1111111111111",
            is_residential=True,
            is_data_ready=False,
        )

    def test_changelist_shows_contract_fields(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:data_propertyrecord_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "9876543210123")
        self.assertContains(response, "1111111111111")

    def test_changelist_filters_by_is_data_ready(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(
            reverse("admin:data_propertyrecord_changelist"), {"is_data_ready__exact": "1"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "9876543210123")
        self.assertNotContains(response, "1111111111111")

    def test_changelist_searches_account_number_by_prefix(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:data_propertyrecord_changelist"), {"q": "987"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "9876543210123")
        self.assertNotContains(response, "1111111111111")

    def test_changelist_searches_address_mid_string(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:data_propertyrecord_changelist"), {"q": "Oak"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1111111111111")
        self.assertNotContains(response, "9876543210123")


class DownloadRecordAdminFilterTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        user_manager = cast(UserManager, user_model.objects)
        self.superuser = user_manager.create_superuser(
            username="download-admin",
            email="download-admin@example.com",
            password="password123",
        )
        DownloadRecord.objects.create(
            url="https://download.hcad.org/data/Real_acct_owner.zip",
            filename="Real_acct_owner.zip",
            extracted=True,
        )
        DownloadRecord.objects.create(
            url="https://download.hcad.org/data/Parcels.zip",
            filename="Parcels.zip",
            extracted=False,
        )

    def test_changelist_search_by_filename(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(
            reverse("admin:data_downloadrecord_changelist"), {"q": "Real_acct_owner"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Real_acct_owner.zip")
        self.assertNotContains(response, "Parcels.zip")


class TaxUnitRateAdminTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        user_manager = cast(UserManager, user_model.objects)
        self.superuser = user_manager.create_superuser(
            username="rate-admin",
            email="rate-admin@example.com",
            password="password123",
        )
        self.rate = TaxUnitRate.objects.create(
            tax_year=2026,
            tax_unit_code="041",
            tax_unit_name="Houston ISD",
            adopted_rate=Decimal("0.96500000"),
        )

    def test_changelist_renders(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:data_taxunitrate_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Houston ISD")

    def test_add_and_delete_blocked_over_http(self) -> None:
        self.client.force_login(self.superuser)

        add_response = self.client.get(reverse("admin:data_taxunitrate_add"))
        delete_response = self.client.get(
            reverse("admin:data_taxunitrate_delete", args=[self.rate.pk])
        )

        self.assertEqual(add_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)

    def test_change_is_read_only_over_http(self) -> None:
        self.client.force_login(self.superuser)

        post_response = self.client.post(
            reverse("admin:data_taxunitrate_change", args=[self.rate.pk]),
            {"adopted_rate": "0.01"},
        )

        self.assertEqual(post_response.status_code, 403)
        self.rate.refresh_from_db()
        self.assertEqual(self.rate.adopted_rate, Decimal("0.96500000"))


class PropertyJurisdictionExemptionAdminTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        user_manager = cast(UserManager, user_model.objects)
        self.superuser = user_manager.create_superuser(
            username="exemption-admin",
            email="exemption-admin@example.com",
            password="password123",
        )
        self.exemption = PropertyJurisdictionExemption.objects.create(
            account_number="5555555555555",
            tax_year=2026,
            tax_unit_code="041",
            exemption_code="HS",
            taxable_value=Decimal("200000.00"),
        )

    def test_changelist_renders(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:data_propertyjurisdictionexemption_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "5555555555555")

    def test_add_and_delete_blocked_over_http(self) -> None:
        self.client.force_login(self.superuser)

        add_response = self.client.get(reverse("admin:data_propertyjurisdictionexemption_add"))
        delete_response = self.client.get(
            reverse("admin:data_propertyjurisdictionexemption_delete", args=[self.exemption.pk])
        )

        self.assertEqual(add_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)

    def test_change_is_read_only_over_http(self) -> None:
        self.client.force_login(self.superuser)

        post_response = self.client.post(
            reverse("admin:data_propertyjurisdictionexemption_change", args=[self.exemption.pk]),
            {"taxable_value": "1.00"},
        )

        self.assertEqual(post_response.status_code, 403)
        self.exemption.refresh_from_db()
        self.assertEqual(self.exemption.taxable_value, Decimal("200000.00"))


class DataHealthDashboardTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        user_model = get_user_model()
        user_manager = cast(UserManager, user_model.objects)
        self.superuser = user_manager.create_superuser(
            username="health-admin",
            email="health-admin@example.com",
            password="password123",
        )
        self.current_year = timezone.now().year

    def test_index_page_shows_zero_rows_when_tax_data_missing(self) -> None:
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data Health")
        self.assertContains(response, "0 rows")
        self.assertContains(response, "import_tax_unit_rates")
        self.assertContains(response, "import_jur_exemptions")

    def test_index_page_shows_populated_counts(self) -> None:
        TaxUnitRate.objects.create(
            tax_year=self.current_year,
            tax_unit_code="041",
            tax_unit_name="Houston ISD",
            adopted_rate=Decimal("0.96500000"),
        )
        PropertyJurisdictionExemption.objects.create(
            account_number="5555555555555",
            tax_year=self.current_year,
            tax_unit_code="041",
            exemption_code="HS",
        )
        PropertyRecord.objects.create(
            address="123 Main St",
            account_number="1111111111111",
            is_residential=True,
            is_data_ready=False,
        )
        AssessmentHistory.objects.create(
            account_number="1111111111111",
            tax_year=self.current_year,
        )
        DownloadRecord.objects.create(
            url="https://download.hcad.org/data/Real_acct_owner.txt",
            filename="Real_acct_owner.txt",
            extracted=True,
        )
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 row")
        self.assertContains(response, str(self.current_year))
        self.assertContains(response, "Real_acct_owner.txt")

    def test_result_is_cached_across_calls(self) -> None:
        from data.templatetags.admin_extras import DATA_HEALTH_CACHE_KEY, data_health_summary

        first = data_health_summary()
        TaxUnitRate.objects.create(
            tax_year=self.current_year,
            tax_unit_code="041",
            adopted_rate=Decimal("0.5"),
        )
        second = data_health_summary()

        self.assertEqual(first["tax_unit_rate_count"], second["tax_unit_rate_count"])
        self.assertIsNotNone(cache.get(DATA_HEALTH_CACHE_KEY))
