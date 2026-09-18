"""Harris publication through authoritative imports and admin requests."""

import csv
import io
import tempfile
import threading
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisImportStatus,
    HarrisPrepare,
    HarrisPreview,
    InvalidHarrisImportRequest,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
from counties.harris.models import BuildingDetail, PropertyRecord


def harris_request(root, key="P100", *, prepare=False):
    import geopandas as gpd
    from shapely.geometry import Point

    base = Path(root) / "extracted"
    for name in ("Real_acct_owner", "Real_building_land", "Parcels"):
        (base / name).mkdir(parents=True, exist_ok=True)
    (base / "Real_acct_owner/real_acct.txt").write_text(
        f"acct\tstate_class\ttot_appr_val\n{key}\tA1\t250000\n"
    )
    (base / "Real_building_land/building_res.txt").write_text(
        f"acct\tbld_num\theat_ar\n{key}\t1\t1800\n"
    )
    (base / "Real_building_land/fixtures.txt").write_text(
        f"acct\tbld_num\ttype\tunits\n{key}\t1\tRMB\t3\n{key}\t1\tRMF\t2\n"
    )
    (base / "Real_building_land/extra_features.txt").write_text(
        f"acct\tbld_num\tcd\n{key}\t1\tGAR\n"
    )
    gpd.GeoDataFrame({"ACCT": [key]}, geometry=[Point(3100000, 13800000)], crs="EPSG:2278").to_file(
        base / "Parcels/parcels.shp"
    )
    return HarrisImportRequest(
        plan=HarrisImportPlan.from_legacy_scope("full"),
        data_year=2026,
        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
        extraction=HarrisExtractionMode.REUSE_EXTRACTED,
        load=(HarrisPrepare if prepare else HarrisApply)(validate_completeness=False),
    )


class HarrisPublicationTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.filter(county="harris"):
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def baseline(self):
        prop = PropertyRecord.objects.create(
            account_number="P100",
            address="Old address",
            is_residential=True,
            is_data_ready=True,
            latitude=29.7,
            longitude=-95.4,
        )
        BuildingDetail.objects.create(
            property=prop,
            account_number="P100",
            building_number=1,
            bedrooms=3,
            bathrooms=2,
            heat_area=1800,
        )

    def test_qualified_replacement_publishes_and_repeated_application_is_idempotent(self):
        self.baseline()
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            request = harris_request(root)
            result = run_harris_import(request)
            self.assertEqual(result.status, HarrisImportStatus.COMPLETED)
            self.assertTrue(result.wrote_data)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.state, "published")
            self.assertEqual(PropertyRecord.objects.get().address, "")
            repeated = run_harris_import(replace(request, candidate_id=candidate.pk))
            self.assertTrue(repeated.success)
            self.assertFalse(repeated.wrote_data)
            self.assertTrue(repeated.already_applied)
            self.assertEqual(
                ImportAuditEntry.objects.filter(kind="publication", result="published").count(), 1
            )

    def test_gis_refresh_preserves_property_year_in_reports_and_exports(self):
        from decimal import Decimal

        import geopandas as gpd
        from shapely.geometry import Point

        self.client.force_login(
            get_user_model().objects.create_superuser("year-review", password="test")
        )
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            request = replace(harris_request(root), data_year=2025)
            base = Path(root) / "extracted"
            keys = [f"P{number}" for number in range(4)]
            (base / "Real_acct_owner/real_acct.txt").write_text(
                "acct\tstate_class\ttot_appr_val\n"
                + "".join(f"{key}\tA1\t250000\n" for key in keys)
            )
            (base / "Real_building_land/building_res.txt").write_text(
                "acct\tbld_num\theat_ar\n" + "".join(f"{key}\t1\t1800\n" for key in keys)
            )
            (base / "Real_building_land/fixtures.txt").write_text(
                "acct\tbld_num\ttype\tunits\n"
                + "".join(f"{key}\t1\tRMB\t3\n{key}\t1\tRMF\t2\n" for key in keys)
            )
            (base / "Real_building_land/extra_features.txt").write_text(
                "acct\tbld_num\tcd\n" + "".join(f"{key}\t1\tGAR\n" for key in keys)
            )
            gpd.GeoDataFrame(
                {"ACCT": keys},
                geometry=[Point(3100000 + i, 13800000) for i in range(4)],
                crs="EPSG:2278",
            ).to_file(base / "Parcels/parcels.shp")
            first = run_harris_import(request)
            review = reverse("admin:data_importcandidate_review", args=[first.candidate_id])
            binding = self.client.get(review).context["form"]["binding"].value()
            self.assertEqual(
                self.client.post(
                    review,
                    {"decision": "approved", "reason": "Reviewed source year", "binding": binding},
                ).status_code,
                302,
            )
            self.assertEqual(
                self.client.post(
                    reverse("admin:data_importcandidate_apply", args=[first.candidate_id]),
                    {"reason": "Publish 2025 properties"},
                ).status_code,
                302,
            )
            TaxUnitRate.objects.create(
                county="harris", tax_year=2026, tax_unit_code="A", adopted_rate=Decimal("0.01")
            )
            PropertyJurisdictionExemption.objects.create(
                county="harris",
                tax_year=2026,
                account_number="P0",
                tax_unit_code="A",
                taxable_value=250000,
            )
            refreshed = run_harris_import(
                replace(
                    request, data_year=2026, plan=HarrisImportPlan.from_legacy_scope("gis-only")
                )
            )
            self.assertTrue(refreshed.wrote_data)
            publication = ImportOperation.objects.get(pk=refreshed.operation_id)
            self.assertEqual(publication.publication_after["property_source_year"], 2025)
            report = self.client.get(reverse("protest_analysis", args=["P0"]))
            self.assertEqual(report.context["subject"].tax_year, 2025)
            self.assertEqual(report.context["tax_impact"].tax_year, 2025)
            self.assertNotEqual(report.context["tax_impact"].completeness, "complete")
            exported = self.client.get(reverse("protest_analysis_export", args=["P0"]))
            rows = list(csv.DictReader(io.StringIO(exported.content.decode())))
            self.assertTrue(rows)
            self.assertTrue(all(row["property_source_year"] == "2025" for row in rows))
            pdf = self.client.get(reverse("protest_analysis_pdf", args=["P0"]))
            self.assertIn(b"Property Source Year: 2025", pdf.content)
            current = run_harris_import(replace(request, data_year=2026))
            if current.status is HarrisImportStatus.AWAITING_REVIEW:
                review = reverse("admin:data_importcandidate_review", args=[current.candidate_id])
                binding = self.client.get(review).context["form"]["binding"].value()
                self.assertEqual(
                    self.client.post(
                        review,
                        {
                            "decision": "approved",
                            "reason": "Reviewed 2026 facts",
                            "binding": binding,
                        },
                    ).status_code,
                    302,
                )
                self.assertEqual(
                    self.client.post(
                        reverse("admin:data_importcandidate_apply", args=[current.candidate_id]),
                        {"reason": "Publish 2026 facts"},
                    ).status_code,
                    302,
                )
            self.assertEqual(
                ImportOperation.objects.filter(county="harris", status="published")
                .first()
                .publication_after["property_source_year"],
                2026,
            )
            recovery = reverse("admin:data_importoperation_recover_dataset", args=[publication.pk])
            binding = self.client.get(recovery).context["form"]["binding"].value()
            self.assertEqual(
                self.client.post(
                    recovery, {"reason": "Restore mixed stage years", "binding": binding}
                ).status_code,
                302,
            )
            candidate = ImportOperation.objects.get(origin="admin_recovery").candidate
            self.assertEqual(candidate.evidence["property_source_year"], 2025)
            self.assertEqual(
                self.client.post(
                    reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                    {"reason": "Restore retained 2025 property facts"},
                ).status_code,
                302,
            )
            report = self.client.get(reverse("protest_analysis", args=["P0"]))
            self.assertEqual(report.context["subject"].tax_year, 2025)
            self.assertNotEqual(report.context["tax_impact"].completeness, "complete")

    def test_preview_and_prepare_cannot_apply_a_candidate(self):
        self.baseline()
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            request = harris_request(root, prepare=True)
            result = run_harris_import(request)
            for intent in (HarrisPreview(), HarrisPrepare()):
                with self.subTest(intent=intent), self.assertRaises(InvalidHarrisImportRequest):
                    run_harris_import(
                        replace(request, load=intent, candidate_id=result.candidate_id)
                    )
            self.assertEqual(PropertyRecord.objects.get().address, "Old address")

    def test_first_import_requires_review_then_explicit_admin_apply(self):
        user = get_user_model().objects.create_superuser("operator", password="test")
        self.client.force_login(user)
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            result = run_harris_import(harris_request(root))
            self.assertEqual(result.status, HarrisImportStatus.AWAITING_REVIEW)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            review = reverse("admin:data_importcandidate_review", args=[candidate.pk])
            binding = self.client.get(review).context["form"]["binding"].value()
            self.assertEqual(
                self.client.post(
                    review,
                    {"decision": "approved", "reason": "First import verified", "binding": binding},
                ).status_code,
                302,
            )
            self.assertFalse(PropertyRecord.objects.exists())
            apply_url = reverse("admin:data_importcandidate_apply", args=[candidate.pk])
            self.assertEqual(
                self.client.post(apply_url, {"reason": "Apply reviewed candidate"}).status_code, 302
            )
            candidate.refresh_from_db()
            self.assertEqual(candidate.state, "published")
            self.assertEqual(PropertyRecord.objects.get().account_number, "P100")

    def test_database_failure_preserves_previous_rows_and_candidate(self):
        self.baseline()
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            request = harris_request(root, prepare=True)
            prepared = run_harris_import(request)

            def fail(execute, sql, params, many, context):
                if sql.startswith('INSERT INTO public."data_buildingdetail"'):
                    raise OSError("Publication connection interrupted")
                return execute(sql, params, many, context)

            with connection.execute_wrapper(fail), self.assertRaises(OSError):
                run_harris_import(
                    replace(request, load=HarrisApply(), candidate_id=prepared.candidate_id)
                )
            self.assertEqual(PropertyRecord.objects.get().address, "Old address")
            self.assertEqual(BuildingDetail.objects.get().bedrooms, 3)
            self.assertNotEqual(
                ImportCandidate.objects.get(pk=prepared.candidate_id).state, "published"
            )

    def test_apply_rechecks_sources_and_live_reviewer_permission(self):
        reviewer = get_user_model().objects.create_superuser("reviewer", password="test")
        operator = get_user_model().objects.create_superuser("operator", password="test")
        for changed in ("source", "property_year", "permission"):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                self.client.force_login(reviewer)
                result = run_harris_import(harris_request(root))
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                review_url = reverse("admin:data_importcandidate_review", args=[candidate.pk])
                binding = self.client.get(review_url).context["form"]["binding"].value()
                self.assertEqual(
                    self.client.post(
                        review_url,
                        {
                            "decision": "approved",
                            "reason": "First import verified",
                            "binding": binding,
                        },
                    ).status_code,
                    302,
                )
                if changed == "source":
                    Path(candidate.sources[0]["path"]).write_text("changed")
                    expected = "Retained source changed"
                elif changed == "property_year":
                    candidate.evidence["property_source_year"] = 2025
                    candidate.save(update_fields=["evidence"])
                    expected = "Approved qualification changed"
                else:
                    reviewer.is_superuser = False
                    reviewer.save()
                    expected = "permission is no longer valid"
                self.client.force_login(operator)
                response = self.client.post(
                    reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                    {"reason": "Apply reviewed candidate"},
                )
                self.assertContains(response, expected)
                self.assertFalse(PropertyRecord.objects.exists())

    def test_targeted_command_and_celery_report_staging_and_preserve_published_data(self):
        from counties.harris.tasks_new import run_etl_pipeline

        self.baseline()
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            request = harris_request(root)
            output = StringIO()
            call_command(
                "load_hcad_real_acct",
                str(Path(root) / "extracted/Real_acct_owner/real_acct.txt"),
                stdout=output,
            )
            self.assertIn("Status: blocked; applied: False", output.getvalue())
            self.assertEqual(PropertyRecord.objects.get().address, "Old address")
            with patch.object(run_etl_pipeline, "update_state") as status:
                result = run_etl_pipeline.run(
                    skip_download=True,
                    skip_extract=True,
                    data_year=2026,
                    scope="property-only",
                    validate_contract=False,
                )
                self.assertIn("published data unchanged", status.call_args.kwargs["meta"]["step"])
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["wrote_data"])
            self.assertTrue(result["candidate_id"])
            self.assertEqual(PropertyRecord.objects.get().address, "Old address")

    def test_failed_final_audit_cannot_record_rolled_back_publication(self):
        self.baseline()
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            request = harris_request(root, prepare=True)
            prepared = run_harris_import(request)

            def fail(execute, sql, params, many, context):
                if sql.startswith('INSERT INTO "data_importauditentry"'):
                    raise OSError("Final audit failed")
                return execute(sql, params, many, context)

            with connection.execute_wrapper(fail), self.assertRaises(OSError):
                run_harris_import(
                    replace(request, load=HarrisApply(), candidate_id=prepared.candidate_id)
                )
            self.assertEqual(PropertyRecord.objects.get().address, "Old address")
            self.assertFalse(ImportOperation.objects.filter(status="published").exists())

    def test_shared_request_keeps_old_property_and_building_during_publication(self):
        self.baseline()
        BuildingDetail.objects.update(bedrooms=5, heat_area=2500)
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            request = harris_request(root, prepare=True)
            prepared = run_harris_import(request)
            selected, resume = threading.Event(), threading.Event()
            observations, errors = [], []

            def reader():
                close_old_connections()

                def pause(execute, sql, params, many, context):
                    result = execute(sql, params, many, context)
                    if (
                        sql.startswith("SELECT")
                        and 'FROM "data_propertyrecord"' in sql
                        and not selected.is_set()
                    ):
                        selected.set()
                        if not resume.wait(20):
                            raise TimeoutError("Publication never resumed the reader")
                    return result

                try:
                    with connection.execute_wrapper(pause):
                        response = Client().get(reverse("similar_properties", args=["P100"]))
                        subject = response.context["subject"]
                        observations.append((subject.bedrooms, subject.living_area))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    connection.close()

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                self.assertTrue(selected.wait(20))
                run_harris_import(
                    replace(request, load=HarrisApply(), candidate_id=prepared.candidate_id)
                )
            finally:
                resume.set()
                thread.join(20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(observations, [(5, 2500.0)])
            response = self.client.get(reverse("similar_properties", args=["P100"]))
            self.assertEqual(response.context["subject"].bedrooms, 3)
