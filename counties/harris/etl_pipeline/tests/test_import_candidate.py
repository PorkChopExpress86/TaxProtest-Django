"""Durable candidate preparation keeps published Harris reads available."""

import tempfile
from threading import Event, Thread

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase
from django.urls import reverse

from counties.common.models import ImportCandidate
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportPhase,
    HarrisImportRequest,
    HarrisPrepare,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_building_sources,
    _write_property_source,
)
from counties.harris.models import BuildingDetail, PropertyRecord


class HarrisCandidateTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def request(self):
        return HarrisImportRequest(
            plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
            data_year=2026,
            acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
            extraction=HarrisExtractionMode.REUSE_EXTRACTED,
            load=HarrisPrepare(validate_completeness=False),
        )

    def test_preparation_retains_candidate_and_prior_published_dependents(self):
        old = PropertyRecord.objects.create(
            account_number="OLD",
            address="100 OLD ST",
            street_number="100",
            street_name="OLD ST",
            is_residential=True,
            is_data_ready=True,
        )
        building = BuildingDetail.objects.create(
            property=old, account_number="OLD", building_number=1
        )
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_property_source(root)
            _write_building_sources(root, "P100")
            result = run_harris_import(self.request())
            self.assertEqual(result.status.value, "prepared")
            self.assertFalse(result.wrote_data)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.state, "prepared")
            self.assertEqual(candidate.evidence["population"]["properties"], 1)
            self.assertEqual(candidate.evidence["population"]["buildings"], 1)
            self.assertTrue(candidate.sources)
            self.assertEqual(PropertyRecord.objects.get().pk, old.pk)
            self.assertEqual(BuildingDetail.objects.get().pk, building.pk)
            self.assertContains(self.client.get(reverse("index"), {"street_name": "OLD"}), "OLD ST")
        connection.close()
        candidate.refresh_from_db()
        self.client.force_login(
            get_user_model().objects.create_superuser("reviewer", password="test")
        )
        response = self.client.get(
            reverse("admin:data_importcandidate_change", args=[candidate.pk])
        )
        self.assertContains(response, "prepared")
        self.assertContains(response, "Published data unchanged")

    def test_shared_reads_continue_while_candidate_load_is_paused(self):
        PropertyRecord.objects.create(
            account_number="OLD",
            address="100 OLD ST",
            street_number="100",
            street_name="OLD ST",
            is_residential=True,
            is_data_ready=True,
        )
        held, release = Event(), Event()
        results, errors = [], []

        class Reporter:
            def report(self, event):
                if event.phase is HarrisImportPhase.LOAD:
                    held.set()
                    if not release.wait(20):
                        raise RuntimeError("Test release did not arrive")

        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_property_source(root)
            _write_building_sources(root, "P100")

            def run():
                try:
                    results.append(run_harris_import(self.request(), reporter=Reporter()))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    connections.close_all()

            worker = Thread(target=run)
            worker.start()
            try:
                self.assertTrue(held.wait(15))
                self.assertContains(
                    self.client.get(reverse("index"), {"street_name": "OLD"}), "OLD ST"
                )
                self.assertEqual(PropertyRecord.objects.get().account_number, "OLD")
            finally:
                release.set()
                worker.join(25)
            self.assertEqual(errors, [])
            self.assertEqual(results[0].status.value, "prepared")

    def test_interruption_after_property_stage_preserves_published_rows(self):
        old = PropertyRecord.objects.create(
            account_number="OLD", address="100 OLD ST", is_residential=True, is_data_ready=True
        )
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_property_source(root)
            _write_building_sources(root, "P100")

            def interrupt(execute, sql, params, many, context):
                if sql.startswith('TRUNCATE TABLE "data_buildingdetail"'):
                    raise OSError("Interrupted candidate building stage")
                return execute(sql, params, many, context)

            with connection.execute_wrapper(interrupt):
                result = run_harris_import(self.request())
            self.assertEqual(result.status.value, "failed")
            self.assertEqual(PropertyRecord.objects.get().pk, old.pk)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.state, "blocked")
            self.assertIn("Interrupted", str(candidate.evidence))

    def test_legacy_serial_public_sequence_is_not_consumed_by_preparation(self):
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE public.data_propertyrecord ALTER COLUMN id DROP IDENTITY")
            cursor.execute(
                "CREATE SEQUENCE public.data_propertyrecord_id_seq OWNED BY public.data_propertyrecord.id"
            )
            cursor.execute(
                "ALTER TABLE public.data_propertyrecord ALTER COLUMN id SET DEFAULT nextval('public.data_propertyrecord_id_seq')"
            )
            cursor.execute("SELECT setval('public.data_propertyrecord_id_seq', 9000, true)")
        try:
            with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
                _write_property_source(root)
                _write_building_sources(root, "P100")
                result = run_harris_import(self.request())
                self.assertEqual(result.status.value, "prepared")
                with connection.cursor() as cursor:
                    cursor.execute("SELECT last_value FROM public.data_propertyrecord_id_seq")
                    self.assertEqual(cursor.fetchone()[0], 9000)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE public.data_propertyrecord ALTER COLUMN id DROP DEFAULT"
                )
                cursor.execute("DROP SEQUENCE public.data_propertyrecord_id_seq")
                cursor.execute(
                    "ALTER TABLE public.data_propertyrecord ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY"
                )
