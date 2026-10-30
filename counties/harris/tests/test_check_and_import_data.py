"""Container-start data check: import only when nothing is published or pending."""

import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from counties.common.county_registry import registration_for
from counties.common.models import CountyWriter, ImportCandidate, ImportOperation
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

COMMAND = "counties.harris.management.commands.check_and_import_data.call_command"
LOCAL_SOURCES = (
    HcadSourceId.REAL_ACCOUNT_OWNER,
    HcadSourceId.REAL_BUILDING_LAND,
    HcadSourceId.GIS_PARCELS,
)


class CheckAndImportDataTests(TestCase):
    def run_check(self, extracted=()):
        out = StringIO()
        with (
            tempfile.TemporaryDirectory() as extract_dir,
            self.settings(HCAD_EXTRACT_DIR=extract_dir),
            patch(COMMAND) as mocked_import,
        ):
            for source_id in extracted:
                dirname = DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(source_id)
                (Path(extract_dir) / dirname).mkdir()
            call_command("check_and_import_data", stdout=out)
        return mocked_import, out.getvalue()

    def test_empty_database_starts_a_full_import(self):
        mocked_import, _ = self.run_check()

        mocked_import.assert_called_once_with("import_all_data")

    def test_extracted_catalog_sources_skip_download_and_extract(self):
        mocked_import, output = self.run_check(extracted=LOCAL_SOURCES)

        mocked_import.assert_called_once_with(
            "import_all_data", skip_download=True, skip_extract=True
        )
        self.assertIn("Local extracted files found", output)

    def test_any_missing_catalog_source_falls_back_to_download(self):
        for missing in LOCAL_SOURCES:
            with self.subTest(missing=missing):
                present = [source_id for source_id in LOCAL_SOURCES if source_id is not missing]

                mocked_import, output = self.run_check(extracted=present)

                mocked_import.assert_called_once_with("import_all_data")
                self.assertIn("Local data files NOT found", output)

    def test_pending_harris_candidate_holds_the_import_and_names_the_next_step(self):
        next_steps = {
            "awaiting_review": "Review it",
            "approved": "Apply it",
            "prepared": "Apply it",
        }
        for state, next_step in next_steps.items():
            with self.subTest(state=state):
                candidate = ImportCandidate.objects.create(
                    county="harris",
                    operation=ImportOperation.objects.create(
                        county="harris", intent="full", status="running"
                    ),
                    state=state,
                    storage_schema=f"harris_candidate_{uuid4().hex}",
                )

                mocked_import, output = self.run_check()

                mocked_import.assert_not_called()
                self.assertIn(f"Harris Candidate {candidate.pk} is {state}", output)
                self.assertIn(next_step, output)
                self.assertIn(
                    reverse("admin:data_importcandidate_change", args=[candidate.pk]), output
                )
                candidate.delete()

    def test_settled_or_other_county_candidates_do_not_hold_the_import(self):
        for county, state in (
            ("harris", "blocked"),
            ("harris", "rejected"),
            ("brazos", "prepared"),
        ):
            ImportCandidate.objects.create(
                county=county,
                operation=ImportOperation.objects.create(
                    county=county, intent="full", status="blocked"
                ),
                state=state,
                storage_schema=f"{county}_candidate_{uuid4().hex}",
            )

        mocked_import, _ = self.run_check()

        mocked_import.assert_called_once_with("import_all_data")

    def preparation(self):
        operation = ImportOperation.objects.create(county="harris", intent="full")
        candidate = ImportCandidate.objects.create(
            county="harris",
            operation=operation,
            state="preparing",
            storage_schema=f"harris_candidate_{uuid4().hex}",
        )
        return operation, candidate

    def test_a_live_preparation_holds_the_import(self):
        operation, candidate = self.preparation()
        key = registration_for("harris").writer_lock_key
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid(), pg_advisory_lock(%s)", [key])
            pid = cursor.fetchone()[0]
        self.addCleanup(lambda: connection.cursor().execute("SELECT pg_advisory_unlock(%s)", [key]))
        CountyWriter.objects.create(county="harris", operation=operation, backend_pid=pid)

        mocked_import, output = self.run_check()

        mocked_import.assert_not_called()
        candidate.refresh_from_db()
        self.assertEqual(candidate.state, "preparing")
        self.assertIn(f"Harris Candidate {candidate.pk} is preparing", output)
        self.assertIn("Wait for it to finish", output)

    def test_a_dead_preparation_is_blocked_and_the_import_starts(self):
        operation, candidate = self.preparation()

        mocked_import, _ = self.run_check()

        mocked_import.assert_called_once_with("import_all_data")
        operation.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual((operation.status, candidate.state), ("failed", "blocked"))

    def test_a_dead_preparation_still_holding_the_writer_names_writer_recovery(self):
        operation, candidate = self.preparation()
        CountyWriter.objects.create(county="harris", operation=operation, backend_pid=None)

        mocked_import, output = self.run_check()

        mocked_import.assert_not_called()
        self.assertIn(f"Harris Candidate {candidate.pk} is preparing", output)
        self.assertIn(
            "Recover the county writer in Django admin "
            + reverse("admin:data_importoperation_recover_writer", args=[operation.pk]),
            output,
        )
        self.assertNotIn("Wait for it to finish", output)
