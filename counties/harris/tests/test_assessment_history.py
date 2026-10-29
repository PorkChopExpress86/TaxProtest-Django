from __future__ import annotations

import dataclasses
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.db import IntegrityError
from django.test import TestCase

from counties.common.tax_models import AssessmentHistory
from counties.harris.etl_pipeline.row_reader import SourceLayoutError
from counties.harris.models import PropertyRecord
from counties.harris.replacement_guard import ReplacementShrinkError
from counties.harris.source_catalog import (
    DEFAULT_HCAD_SOURCE_CATALOG,
    HcadSourceCatalog,
    HcadSourceId,
)

REAL_ACCT_DIR = DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(HcadSourceId.REAL_ACCOUNT_OWNER)
HEARING_DIR = DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(HcadSourceId.HEARING_FILES)


class AssessmentHistoryModelTests(TestCase):
    def test_reverse_relation_orders_by_newest_year_first(self):
        AssessmentHistory.objects.create(
            account_number="HIST001",
            tax_year=2025,
            assessed_value=Decimal("240000"),
        )
        AssessmentHistory.objects.create(
            account_number="HIST001",
            tax_year=2026,
            assessed_value=Decimal("260000"),
        )

        years = list(
            AssessmentHistory.objects.filter(account_number="HIST001").values_list(
                "tax_year", flat=True
            )
        )

        self.assertEqual(years, [2026, 2025])

    def test_unique_constraint_is_account_and_tax_year(self):
        AssessmentHistory.objects.create(
            account_number="HIST002",
            tax_year=2026,
            assessed_value=Decimal("260000"),
        )

        with self.assertRaises(IntegrityError):
            AssessmentHistory.objects.create(
                account_number="HIST002",
                tax_year=2026,
                assessed_value=Decimal("255000"),
            )


class ImportAssessmentHistoryCommandTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.extract_root = Path(self.tempdir.name) / "extract"
        self.download_root = Path(self.tempdir.name) / "download"
        self.extract_root.mkdir(parents=True, exist_ok=True)
        self.download_root.mkdir(parents=True, exist_ok=True)

        self.prop = PropertyRecord.objects.create(
            address="300 History St",
            city="Houston",
            zipcode="77001",
            account_number="HIST300",
            owner_name="History Owner 3",
            assessed_value=Decimal("310000"),
        )

    def _write_year_files(
        self,
        year: int,
        *,
        real_rows: list[str],
        hearing_rows: list[str] | None = None,
    ) -> None:
        real_dir = self.extract_root / str(year) / REAL_ACCT_DIR
        real_dir.mkdir(parents=True, exist_ok=True)
        (real_dir / "real_acct.txt").write_text(
            "acct\tyr\tassessed_val\ttot_appr_val\ttot_mkt_val\tprior_tot_appr_val\tprior_tot_mkt_val\tnew_construction_val\tCap_acct\tprotested\n"
            + "\n".join(real_rows)
            + "\n",
            encoding="utf-8",
        )

        if hearing_rows is not None:
            hearing_dir = self.extract_root / str(year) / HEARING_DIR
            hearing_dir.mkdir(parents=True, exist_ok=True)
            (hearing_dir / "arb_hearings_real.txt").write_text(
                "acct\tTax_Year\tHearing_Type\tScheduled_for_Date\tActual_Hearing_Date\tRelease_Date\tInitial_Appraised_Value\tFinal_Appraised_Value\n"
                + "\n".join(hearing_rows)
                + "\n",
                encoding="utf-8",
            )

    def _import(self, start_year: int, end_year: int) -> None:
        call_command(
            "import_assessment_history",
            "--start-year",
            str(start_year),
            "--end-year",
            str(end_year),
            "--skip-download",
            "--skip-extract",
            "--extract-root",
            str(self.extract_root),
            "--download-root",
            str(self.download_root),
        )

    def _seed_history(self, year: int, accounts: list[str]) -> None:
        for account in accounts:
            AssessmentHistory.objects.create(
                account_number=account,
                tax_year=year,
                county="harris",
                assessed_value=Decimal("100000"),
            )

    def test_renamed_acct_column_raises_and_keeps_the_existing_year(self):
        self._seed_history(2026, ["HIST300", "HIST301"])
        real_dir = self.extract_root / "2026" / REAL_ACCT_DIR
        real_dir.mkdir(parents=True)
        (real_dir / "real_acct.txt").write_text(
            "account\tyr\tassessed_val\nHIST300\t2026\t310000\nHIST301\t2026\t220000\n",
            encoding="utf-8",
        )

        with self.assertRaises(SourceLayoutError) as ctx:
            self._import(2026, 2026)

        self.assertIn("acct", str(ctx.exception))
        self.assertEqual(
            AssessmentHistory.objects.filter(county="harris", tax_year=2026).count(), 2
        )

    def test_reload_far_below_the_existing_year_is_refused(self):
        self._seed_history(2026, ["HIST300", "HIST301", "HIST302", "HIST303"])
        self._write_year_files(
            2026, real_rows=["HIST300\t2026\t310000\t320000\t325000\t280000\t300000\t0\t\tN"]
        )

        with self.assertRaises(ReplacementShrinkError):
            self._import(2026, 2026)

        self.assertEqual(
            set(
                AssessmentHistory.objects.filter(county="harris", tax_year=2026).values_list(
                    "account_number", "assessed_value"
                )
            ),
            {(a, Decimal("100000.00")) for a in ("HIST300", "HIST301", "HIST302", "HIST303")},
        )

    def test_one_shrinking_year_refuses_the_whole_range(self):
        self._seed_history(2025, ["HIST300", "HIST301", "HIST302"])
        self._seed_history(2026, ["HIST300", "HIST301", "HIST302"])
        rows = [f"HIST30{i}\t2025\t1\t1\t1\t1\t1\t0\t\tN" for i in range(3)]
        self._write_year_files(2025, real_rows=rows)
        self._write_year_files(2026, real_rows=["HIST300\t2026\t1\t1\t1\t1\t1\t0\t\tN"])

        with self.assertRaises(ReplacementShrinkError) as ctx:
            self._import(2025, 2026)

        self.assertIn("2026", str(ctx.exception))
        self.assertEqual(
            AssessmentHistory.objects.filter(
                county="harris", assessed_value=Decimal("100000")
            ).count(),
            6,
        )

    def test_embedded_quote_does_not_merge_rows(self):
        self._write_year_files(
            2026,
            real_rows=[
                'HIST300\t2026\t310000\t320000\t325000\t280000\t300000\t0\t\t"Y',
                "HIST301\t2026\t220000\t230000\t235000\t200000\t210000\t0\t\tN",
            ],
        )

        self._import(2026, 2026)

        self.assertEqual(
            AssessmentHistory.objects.get(account_number="HIST301", tax_year=2026).assessed_value,
            Decimal("220000"),
        )

    def test_import_command_prefers_hearing_final_value(self):
        self._write_year_files(
            2026,
            real_rows=["HIST300\t2026\t310000\t320000\t325000\t280000\t300000\t5000\tY\tY"],
            hearing_rows=["HIST300\t2026\tI\t04/09/2026\t04/08/2026\t04/15/2026\t310000\t295000"],
        )

        call_command(
            "import_assessment_history",
            "--start-year",
            "2026",
            "--end-year",
            "2026",
            "--skip-download",
            "--skip-extract",
            "--extract-root",
            str(self.extract_root),
            "--download-root",
            str(self.download_root),
        )

        history = AssessmentHistory.objects.get(account_number="HIST300", tax_year=2026)
        self.assertEqual(history.assessed_value, Decimal("295000"))
        self.assertEqual(history.appraised_value, Decimal("320000"))
        self.assertEqual(history.market_value, Decimal("325000"))
        self.assertEqual(history.prior_appraised_value, Decimal("280000"))
        self.assertEqual(history.prior_market_value, Decimal("300000"))
        self.assertEqual(history.new_construction_value, Decimal("5000"))
        self.assertEqual(history.cap_account, "Y")

    def test_import_command_keeps_real_acct_value_when_hearing_final_blank(self):
        self._write_year_files(
            2025,
            real_rows=["HIST300\t2025\t280000\t285000\t290000\t260000\t270000\t0\t\tN"],
            hearing_rows=["HIST300\t2025\tI\t04/09/2025\t04/08/2025\t04/15/2025\t280000\t"],
        )

        call_command(
            "import_assessment_history",
            "--start-year",
            "2025",
            "--end-year",
            "2025",
            "--skip-download",
            "--skip-extract",
            "--extract-root",
            str(self.extract_root),
            "--download-root",
            str(self.download_root),
        )

        history = AssessmentHistory.objects.get(account_number="HIST300", tax_year=2025)
        self.assertEqual(history.assessed_value, Decimal("280000"))

    def test_import_command_handles_hearing_only_accounts(self):
        PropertyRecord.objects.create(
            address="301 History St",
            city="Houston",
            zipcode="77001",
            account_number="HIST301",
            owner_name="History Owner 4",
            assessed_value=Decimal("220000"),
        )
        self._write_year_files(
            2024,
            real_rows=["HIST300\t2024\t260000\t265000\t270000\t240000\t250000\t0\t\tN"],
            hearing_rows=["HIST301\t2024\tI\t04/09/2024\t04/08/2024\t04/15/2024\t230000\t210000"],
        )

        call_command(
            "import_assessment_history",
            "--start-year",
            "2024",
            "--end-year",
            "2024",
            "--skip-download",
            "--skip-extract",
            "--extract-root",
            str(self.extract_root),
            "--download-root",
            str(self.download_root),
        )

        hearing_only = AssessmentHistory.objects.get(account_number="HIST301", tax_year=2024)
        self.assertEqual(hearing_only.assessed_value, Decimal("210000"))

    def test_import_command_is_idempotent_for_same_year_range(self):
        self._write_year_files(
            2026,
            real_rows=["HIST300\t2026\t310000\t320000\t325000\t280000\t300000\t5000\tY\tY"],
            hearing_rows=["HIST300\t2026\tI\t04/09/2026\t04/08/2026\t04/15/2026\t310000\t295000"],
        )

        for _ in range(2):
            call_command(
                "import_assessment_history",
                "--start-year",
                "2026",
                "--end-year",
                "2026",
                "--skip-download",
                "--skip-extract",
                "--extract-root",
                str(self.extract_root),
                "--download-root",
                str(self.download_root),
            )

        self.assertEqual(
            AssessmentHistory.objects.filter(account_number="HIST300", tax_year=2026).count(),
            1,
        )

    def test_import_command_only_rebuilds_requested_years(self):
        self._write_year_files(
            2025,
            real_rows=["HIST300\t2025\t280000\t285000\t290000\t260000\t270000\t0\t\tN"],
            hearing_rows=[],
        )
        self._write_year_files(
            2026,
            real_rows=["HIST300\t2026\t310000\t320000\t325000\t280000\t300000\t5000\tY\tY"],
            hearing_rows=["HIST300\t2026\tI\t04/09/2026\t04/08/2026\t04/15/2026\t310000\t295000"],
        )

        call_command(
            "import_assessment_history",
            "--start-year",
            "2025",
            "--end-year",
            "2026",
            "--skip-download",
            "--skip-extract",
            "--extract-root",
            str(self.extract_root),
            "--download-root",
            str(self.download_root),
        )

        AssessmentHistory.objects.filter(account_number="HIST300", tax_year=2025).update(
            assessed_value=Decimal("281000")
        )

        call_command(
            "import_assessment_history",
            "--start-year",
            "2026",
            "--end-year",
            "2026",
            "--skip-download",
            "--skip-extract",
            "--extract-root",
            str(self.extract_root),
            "--download-root",
            str(self.download_root),
        )

        self.assertEqual(
            AssessmentHistory.objects.get(account_number="HIST300", tax_year=2025).assessed_value,
            Decimal("281000"),
        )
        self.assertEqual(
            AssessmentHistory.objects.get(account_number="HIST300", tax_year=2026).assessed_value,
            Decimal("295000"),
        )

    def _with_renamed_display_names(self):
        renamed = HcadSourceCatalog(
            sources=tuple(
                dataclasses.replace(source, name=f"Renamed {source.name}")
                for source in DEFAULT_HCAD_SOURCE_CATALOG.sources
            )
        )
        return patch(
            "counties.harris.management.commands.import_assessment_history"
            ".DEFAULT_HCAD_SOURCE_CATALOG",
            renamed,
        )

    def _write_archive(self, year: int, source_id: HcadSourceId, members: dict[str, str]):
        archive_dir = self.download_root / str(year)
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(source_id).filename
        if members:
            with zipfile.ZipFile(path, "w") as archive:
                for name, text in members.items():
                    archive.writestr(name, text)
        else:
            path.write_text("not a zip archive", encoding="utf-8")

    def _import_with_extract(self, year: int) -> None:
        call_command(
            "import_assessment_history",
            "--start-year",
            str(year),
            "--end-year",
            str(year),
            "--skip-download",
            "--extract-root",
            str(self.extract_root),
            "--download-root",
            str(self.download_root),
        )

    def test_import_selects_sources_by_id_not_display_name(self):
        self._write_year_files(
            2026,
            real_rows=["HIST300\t2026\t310000\t320000\t325000\t280000\t300000\t5000\tY\tY"],
        )

        with self._with_renamed_display_names():
            self._import(2026, 2026)

        self.assertEqual(
            AssessmentHistory.objects.get(account_number="HIST300", tax_year=2026).assessed_value,
            Decimal("310000"),
        )

    def test_a_failed_real_account_extract_fails_the_import(self):
        self._write_archive(2026, HcadSourceId.REAL_ACCOUNT_OWNER, {})

        with self._with_renamed_display_names(), self.assertRaises(CommandError) as ctx:
            self._import_with_extract(2026)

        self.assertIn("Failed to extract Real Account Owner for 2026", str(ctx.exception))
        self.assertFalse(AssessmentHistory.objects.filter(county="harris").exists())

    def test_a_failed_hearing_extract_does_not_fail_the_import(self):
        self._write_archive(
            2026,
            HcadSourceId.REAL_ACCOUNT_OWNER,
            {"real_acct.txt": "acct\tyr\tassessed_val\nHIST300\t2026\t310000\n"},
        )
        self._write_archive(2026, HcadSourceId.HEARING_FILES, {})

        with self._with_renamed_display_names():
            self._import_with_extract(2026)

        self.assertEqual(
            AssessmentHistory.objects.get(account_number="HIST300", tax_year=2026).assessed_value,
            Decimal("310000"),
        )
