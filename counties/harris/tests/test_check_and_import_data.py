"""Container-start data check: import only when nothing is published or pending."""

import tempfile
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from counties.common.models import ImportCandidate, ImportOperation

COMMAND = "counties.harris.management.commands.check_and_import_data.call_command"


class CheckAndImportDataTests(TestCase):
    def run_check(self):
        out = StringIO()
        with (
            tempfile.TemporaryDirectory() as extract_dir,
            self.settings(HCAD_EXTRACT_DIR=extract_dir),
            patch(COMMAND) as mocked_import,
        ):
            call_command("check_and_import_data", stdout=out)
        return mocked_import, out.getvalue()

    def test_empty_database_starts_a_full_import(self):
        mocked_import, _ = self.run_check()

        mocked_import.assert_called_once_with("import_all_data")

    def test_pending_harris_candidate_holds_the_import_and_names_the_next_step(self):
        next_steps = {
            "preparing": "Wait for it to finish",
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
