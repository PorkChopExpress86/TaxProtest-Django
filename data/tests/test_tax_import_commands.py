from __future__ import annotations

import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from data.models import PropertyJurisdictionExemption, TaxUnitRate


class TaxImportCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_import_tax_unit_rates_upserts_by_year_and_code(self):
        p = self.root / "rates.tsv"
        p.write_text(
            "tax_unit_code\ttax_unit_name\tadopted_rate\n"
            "U1\tUnit One\t0.0200\n",
            encoding="utf-8",
        )

        call_command("import_tax_unit_rates", "--path", str(p), "--tax-year", "2026")
        self.assertEqual(TaxUnitRate.objects.count(), 1)
        self.assertEqual(str(TaxUnitRate.objects.get().adopted_rate), "0.02000000")

        p.write_text(
            "tax_unit_code\ttax_unit_name\tadopted_rate\n"
            "U1\tUnit One Updated\t0.0210\n",
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
