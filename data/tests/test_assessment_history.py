from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase

from data.assessment_history import evaluate_cap_status
from data.models import AssessmentHistory, PropertyRecord


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

    def test_cap_status_homestead_uses_ten_percent_plus_new_construction(self):
        prior = AssessmentHistory.objects.create(
            account_number="HIST003",
            tax_year=2025,
            assessed_value=Decimal("300000"),
            appraised_value=Decimal("300000"),
            market_value=Decimal("330000"),
        )
        current = AssessmentHistory.objects.create(
            account_number="HIST003",
            tax_year=2026,
            assessed_value=Decimal("341000"),
            appraised_value=Decimal("341000"),
            market_value=Decimal("380000"),
            prior_appraised_value=Decimal("300000"),
            new_construction_value=Decimal("10000"),
            cap_account="Y",
        )

        status = evaluate_cap_status(current, prior)

        self.assertEqual(status["cap_type"], "homestead")
        self.assertEqual(status["limit_percent"], Decimal("10"))
        self.assertEqual(status["allowed_value"], Decimal("340000.00"))
        self.assertEqual(status["status"], "over_limit")
        self.assertEqual(status["increase_percent"], Decimal("13.67"))

    def test_cap_status_non_homestead_circuit_breaker_uses_twenty_percent(self):
        prior = AssessmentHistory.objects.create(
            account_number="HIST004",
            tax_year=2025,
            assessed_value=Decimal("400000"),
            appraised_value=Decimal("400000"),
            market_value=Decimal("430000"),
        )
        current = AssessmentHistory.objects.create(
            account_number="HIST004",
            tax_year=2026,
            assessed_value=Decimal("470000"),
            appraised_value=Decimal("470000"),
            market_value=Decimal("600000"),
            prior_appraised_value=Decimal("400000"),
            new_construction_value=Decimal("0"),
            cap_account="",
        )

        status = evaluate_cap_status(current, prior)

        self.assertEqual(status["cap_type"], "circuit_breaker")
        self.assertEqual(status["limit_percent"], Decimal("20"))
        self.assertEqual(status["allowed_value"], Decimal("480000.00"))
        self.assertEqual(status["status"], "within_limit")
        self.assertEqual(status["increase_percent"], Decimal("17.50"))


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
        real_dir = self.extract_root / str(year) / "Real_acct_owner"
        real_dir.mkdir(parents=True, exist_ok=True)
        (real_dir / "real_acct.txt").write_text(
            "acct\tyr\tassessed_val\ttot_appr_val\ttot_mkt_val\tprior_tot_appr_val\tprior_tot_mkt_val\tnew_construction_val\tCap_acct\tprotested\n"
            + "\n".join(real_rows)
            + "\n",
            encoding="utf-8",
        )

        if hearing_rows is not None:
            hearing_dir = self.extract_root / str(year) / "Hearing_files"
            hearing_dir.mkdir(parents=True, exist_ok=True)
            (hearing_dir / "arb_hearings_real.txt").write_text(
                "acct\tTax_Year\tHearing_Type\tScheduled_for_Date\tActual_Hearing_Date\tRelease_Date\tInitial_Appraised_Value\tFinal_Appraised_Value\n"
                + "\n".join(hearing_rows)
                + "\n",
                encoding="utf-8",
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
