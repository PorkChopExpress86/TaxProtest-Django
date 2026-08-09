from __future__ import annotations

import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase

from counties.harris.models import PropertyJurisdictionExemption, TaxUnitRate


class TaxImportCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_import_tax_unit_rates_upserts_by_year_and_code(self):
        p = self.root / "rates.tsv"
        p.write_text(
            "tax_unit_code\ttax_unit_name\tadopted_rate\n" "U1\tUnit One\t0.0200\n",
            encoding="utf-8",
        )

        call_command("import_tax_unit_rates", "--path", str(p), "--tax-year", "2026")
        self.assertEqual(TaxUnitRate.objects.count(), 1)
        self.assertEqual(str(TaxUnitRate.objects.get().adopted_rate), "0.02000000")

        p.write_text(
            "tax_unit_code\ttax_unit_name\tadopted_rate\n" "U1\tUnit One Updated\t0.0210\n",
            encoding="utf-8",
        )
        call_command("import_tax_unit_rates", "--path", str(p), "--tax-year", "2026")

        row = TaxUnitRate.objects.get(tax_year=2026, tax_unit_code="U1")
        self.assertEqual(TaxUnitRate.objects.count(), 1)
        self.assertEqual(str(row.adopted_rate), "0.02100000")

    def test_import_jur_exemptions_upserts_rows(self):
        p = self.root / "jur.tsv"
        p.write_text(
            "account_number\ttax_unit_code\ttax_unit_name\texemption_code\texemption_amount\ttaxable_value\n"
            "A1\tU1\tUnit One\tHS\t40000\t300000\n",
            encoding="utf-8",
        )

        call_command("import_jur_exemptions", "--path", str(p), "--tax-year", "2026")
        self.assertEqual(PropertyJurisdictionExemption.objects.count(), 1)

        p.write_text(
            "account_number\ttax_unit_code\ttax_unit_name\texemption_code\texemption_amount\ttaxable_value\n"
            "A1\tU1\tUnit One\tHS\t45000\t290000\n",
            encoding="utf-8",
        )
        call_command("import_jur_exemptions", "--path", str(p), "--tax-year", "2026")

        row = PropertyJurisdictionExemption.objects.get(
            account_number="A1", tax_year=2026, tax_unit_code="U1", exemption_code="HS"
        )
        self.assertEqual(PropertyJurisdictionExemption.objects.count(), 1)
        self.assertEqual(str(row.exemption_amount), "45000.00")


class ImportJurExemptionsGuardTests(TestCase):
    """A file whose columns none of the aliases match must not look like success."""

    def _fixture(self, text: str) -> str:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_hcad_raw_columns_raise_instead_of_reporting_zero_upserts(self):
        # HCAD's own jur_value.txt header: tax_district, not tax_unit_code.
        path = self._fixture(
            "acct\ttax_district\ttp_cd\tpct_district\tappraised_val\ttaxable_val\n"
            "0010010000013\t001\tI\t1.0000\t1217073\t0\n"
        )

        with self.assertRaises(CommandError) as ctx:
            call_command("import_jur_exemptions", "--path", path, "--tax-year", "2025")

        message = str(ctx.exception)
        self.assertIn("Every one of 1 rows was skipped", message)
        self.assertIn("import_hcad_jur_exempt", message)

    def test_partially_skipped_rows_still_import_and_warn(self):
        path = self._fixture(
            "account_number\ttax_unit_code\texemption_code\n"
            "0010010000013\t001\tRES\n"
            "\t001\tRES\n"
        )
        out = StringIO()
        call_command("import_jur_exemptions", "--path", path, "--tax-year", "2025", stdout=out)

        self.assertIn("Skipped 1 rows", out.getvalue())
        self.assertEqual(PropertyJurisdictionExemption.objects.count(), 1)
