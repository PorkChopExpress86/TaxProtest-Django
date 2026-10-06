"""Characterize what Brazos property-import commands report for real runs.

These pins hold today's exit behaviour and printed text (with its style) before the
commands classify through the Import disposition (ADR-0023).
"""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase

from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount, SnapshotOutcome
from counties.brazos.tests.test_property_coverage import write_gis, write_pacs
from counties.common.models import ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate

OFFLINE_2026 = ("--skip-download", "--skip-extract", "--year", "2026")
ACCOUNTS = [str(number + 10013).zfill(12) for number in range(4)]


def success(text):
    return f"\x1b[32;1m{text}\x1b[0m"


def warning(text):
    return f"\x1b[33;1m{text}\x1b[0m"


class BrazosImportResultTestCase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            self.settings(
                BCAD_DOWNLOAD_DIR=str(self.root / "downloads"),
                BCAD_EXTRACT_DIR=str(self.root / "extracted"),
            )
        )

    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def publish_completed_2025_snapshot(self):
        """A published baseline that a same-population 2026 import qualifies against."""
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
        )
        TaxUnitRate.objects.create(
            county="brazos", tax_year=2025, tax_unit_code="C", adopted_rate="0.01"
        )
        for key in ACCOUNTS:
            PropertyAccount.objects.create(
                prop_id=key,
                tax_year=2025,
                owner_name="Published owner",
                assessed_value=250000,
                living_area=1800,
                class_code="RV3",
                year_built=1980,
                latitude=30.6,
                longitude=-96.3,
                coordinate_source="bcad-certified-gis",
                coordinate_source_year=2025,
            )
            PropertyJurisdictionExemption.objects.create(
                county="brazos",
                tax_year=2025,
                account_number=key,
                tax_unit_code="C",
                taxable_value=250000,
            )

    def call(self, *args):
        output = StringIO()
        call_command(*args, stdout=output, force_color=True)
        return output.getvalue().splitlines()

    def call_failing(self, *args):
        output = StringIO()
        with self.assertRaises(CommandError) as raised:
            call_command(*args, stdout=output, force_color=True)
        self.assertEqual(raised.exception.returncode, 1)
        return str(raised.exception), output.getvalue()

    def finished(self, status, state):
        """The one recorded operation and its candidate, with their persisted states."""
        operation = ImportOperation.objects.get()
        candidate = ImportCandidate.objects.get(pk=operation.evidence["candidate_id"])
        self.assertEqual(operation.status, status)
        self.assertEqual(candidate.state, state)
        return operation, candidate

    @staticmethod
    def held(subject, state, operation, candidate):
        # Printed without a style today.
        return (
            f"Brazos {subject} import {state}; published data unchanged. "
            f"Candidate {candidate.pk}; operation {operation.pk}. "
            "Review in Django admin /admin/data/importcandidate/."
        )


class LoadBrazosCadResultTests(BrazosImportResultTestCase):
    def test_first_import_is_held_for_review_and_exits_zero(self):
        write_pacs(self.root, ACCOUNTS, equity=True)

        lines = self.call("load_brazos_cad", *OFFLINE_2026)

        operation, candidate = self.finished("awaiting_review", "awaiting_review")
        self.assertEqual(lines[-1], self.held("CAD", "awaiting_review", operation, candidate))

    def test_blocked_candidate_is_held_and_exits_zero(self):
        write_pacs(self.root, ACCOUNTS, owners=False)

        lines = self.call("load_brazos_cad", *OFFLINE_2026)

        operation, candidate = self.finished("blocked", "blocked")
        self.assertEqual(lines[-1], self.held("CAD", "blocked", operation, candidate))

    def test_incomplete_source_fails_with_exit_code_one(self):
        source = self.root / "extracted" / "2026"
        source.mkdir(parents=True)
        (source / "APPRAISAL_INFO.TXT").write_text("incomplete", encoding="utf-8")

        message, output = self.call_failing("load_brazos_cad", *OFFLINE_2026)

        self.assertEqual(
            message,
            "CAD preflight missing required PACS files: APPRAISAL_ENTITY_INFO.TXT, "
            "APPRAISAL_IMPROVEMENT_DETAIL.TXT, APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT, "
            "APPRAISAL_IMPROVEMENT_INFO.TXT, APPRAISAL_LAND_DETAIL.TXT",
        )
        self.assertEqual(output, "")
        self.finished("failed", "blocked")

    def test_qualified_import_publishes_a_partial_snapshot_as_a_warning(self):
        self.publish_completed_2025_snapshot()
        write_pacs(self.root, ACCOUNTS, equity=True)

        lines = self.call("load_brazos_cad", *OFFLINE_2026)

        self.finished("published", "published")
        self.assertEqual(
            lines[-1],
            warning(
                "Brazos CAD recovery published a Partial property snapshot for "
                "tax_year=2026; year-matched GIS is still unavailable."
            ),
        )


class RefreshBrazosAnnualResultTests(BrazosImportResultTestCase):
    def test_first_import_is_held_for_review_and_exits_zero(self):
        write_pacs(self.root, ACCOUNTS, equity=True)
        write_gis(self.root, ACCOUNTS)

        lines = self.call("refresh_brazos_annual", *OFFLINE_2026)

        operation, candidate = self.finished("awaiting_review", "awaiting_review")
        self.assertEqual(lines[-1], self.held("annual", "awaiting_review", operation, candidate))

    def test_missing_gis_source_fails_with_exit_code_one(self):
        write_pacs(self.root, ACCOUNTS, equity=True)

        message, output = self.call_failing("refresh_brazos_annual", *OFFLINE_2026)

        self.assertTrue(
            message.startswith(
                "--skip-download set but no BCAD GIS archive or extracted parcel directory "
                "was found under "
            ),
            message,
        )
        self.assertNotIn("published data unchanged", output)
        self.finished("failed", "blocked")

    def test_qualified_refresh_publishes_and_reports_success(self):
        self.publish_completed_2025_snapshot()
        write_pacs(self.root, ACCOUNTS, equity=True)
        write_gis(self.root, ACCOUNTS)

        lines = self.call("refresh_brazos_annual", *OFFLINE_2026)

        self.finished("published", "published")
        self.assertEqual(
            lines[-1],
            success(
                "Brazos annual refresh complete for tax_year=2026: "
                "CAD 24 rows, GIS 4 rows enriched."
            ),
        )


class LoadBrazosGisResultTests(BrazosImportResultTestCase):
    def test_recovery_of_an_unreviewed_partial_snapshot_is_held_and_exits_zero(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2026, outcome=SnapshotOutcome.PARTIAL, cad_source_year=2026
        )
        for key in ACCOUNTS:
            PropertyAccount.objects.create(prop_id=key, tax_year=2026, owner_name="Partial owner")
        write_gis(self.root, ACCOUNTS)

        lines = self.call("load_brazos_gis", *OFFLINE_2026)

        operation, candidate = self.finished("awaiting_review", "awaiting_review")
        self.assertEqual(lines[-1], self.held("GIS", "awaiting_review", operation, candidate))

    def test_recovery_without_an_active_partial_snapshot_fails_with_exit_code_one(self):
        write_gis(self.root, ACCOUNTS)

        message, output = self.call_failing("load_brazos_gis", *OFFLINE_2026)

        self.assertEqual(
            message, "No active Partial Brazos property snapshot is available for GIS recovery."
        )
        self.assertEqual(output, "")
        self.finished("failed", "blocked")
