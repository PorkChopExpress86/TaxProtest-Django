"""Tests for the import_brazos_tax_rates management command.

Mocks requests.get with a fixture HTML page matching BCAD's real published
table structure (a merged "<year> TAX RATES" heading row, a column-header
row, then one row per jurisdiction formatted "CODE – Name") -- see
docs/research/brazos-entity-tax-rates.md for the real page's exact shape.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from data.models import PropertyJurisdictionExemption, TaxUnitRate
from data.tax_impact import calculate_tax_impact

FIXTURE_HTML = """
<html><body>
<table>
<tr><th colspan="4">2025 TAX RATES</th></tr>
<tr><th>JURISDICTION</th><th>M&amp;O</th><th>I&amp;S</th><th>TOTAL RATE</th></tr>
<tr><td>G1 – Brazos County</td><td>$0.389454</td><td>$0.030246</td><td>$0.419700</td></tr>
<tr><td>S1 – Bryan ISD</td><td>$0.676900</td><td>$0.270000</td><td>$0.946900</td></tr>
</table>
</body></html>
"""


def _mock_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


class ImportBrazosTaxRatesTests(TestCase):
    def test_scrapes_and_upserts_rates_with_year_from_heading(self):
        with patch(
            "brazos_cad.management.commands.import_brazos_tax_rates.requests.get",
            return_value=_mock_response(FIXTURE_HTML),
        ):
            call_command("import_brazos_tax_rates")

        self.assertEqual(TaxUnitRate.objects.filter(county="brazos").count(), 2)
        g1 = TaxUnitRate.objects.get(county="brazos", tax_unit_code="G1", tax_year=2025)
        self.assertEqual(g1.tax_unit_name, "Brazos County")
        # BCAD publishes $0.419700 per $100 of value -- tax_impact.py expects
        # a true fraction (0.02 = 2%), so this must be divided by 100, NOT
        # stored as the raw table value (a real bug caught by manual
        # end-to-end verification: it overcharged every Brazos tax estimate
        # by exactly 100x before this test existed).
        self.assertEqual(g1.adopted_rate, Decimal("0.004197").quantize(Decimal("0.00000000")))
        s1 = TaxUnitRate.objects.get(county="brazos", tax_unit_code="S1", tax_year=2025)
        self.assertEqual(s1.tax_unit_name, "Bryan ISD")
        self.assertEqual(s1.adopted_rate, Decimal("0.009469").quantize(Decimal("0.00000000")))

    def test_rate_produces_correct_tax_impact_math(self):
        """End-to-end regression guard for the /100 bug: a $200,000 taxable
        property at Brazos County's real $0.4197/$100 rate owes $839.40, not
        $83,940."""
        with patch(
            "brazos_cad.management.commands.import_brazos_tax_rates.requests.get",
            return_value=_mock_response(FIXTURE_HTML),
        ):
            call_command("import_brazos_tax_rates")

        PropertyJurisdictionExemption.objects.create(
            account_number="000000010013",
            tax_year=2025,
            county="brazos",
            tax_unit_code="G1",
            tax_unit_name="Brazos County",
            exemption_code="",
            taxable_value=Decimal("200000"),
            assessed_value=Decimal("200000"),
        )

        result = calculate_tax_impact("000000010013", 2025, None, county="brazos")

        self.assertEqual(result.current_tax_owed, Decimal("839.40"))

    def test_explicit_year_overrides_heading(self):
        with patch(
            "brazos_cad.management.commands.import_brazos_tax_rates.requests.get",
            return_value=_mock_response(FIXTURE_HTML),
        ):
            call_command("import_brazos_tax_rates", "--year", "2026")

        self.assertTrue(TaxUnitRate.objects.filter(county="brazos", tax_year=2026).exists())
        self.assertFalse(TaxUnitRate.objects.filter(county="brazos", tax_year=2025).exists())

    def test_dry_run_writes_nothing(self):
        with patch(
            "brazos_cad.management.commands.import_brazos_tax_rates.requests.get",
            return_value=_mock_response(FIXTURE_HTML),
        ):
            call_command("import_brazos_tax_rates", "--dry-run")

        self.assertEqual(TaxUnitRate.objects.count(), 0)

    def test_rerunning_updates_rather_than_duplicates(self):
        with patch(
            "brazos_cad.management.commands.import_brazos_tax_rates.requests.get",
            return_value=_mock_response(FIXTURE_HTML),
        ):
            call_command("import_brazos_tax_rates")
            call_command("import_brazos_tax_rates")

        self.assertEqual(TaxUnitRate.objects.filter(county="brazos").count(), 2)

    def test_does_not_touch_harris_rows_with_same_code(self):
        TaxUnitRate.objects.create(
            tax_year=2025,
            tax_unit_code="G1",
            county="harris",
            tax_unit_name="A Harris unit that happens to share a code",
            adopted_rate=Decimal("0.010000"),
        )
        with patch(
            "brazos_cad.management.commands.import_brazos_tax_rates.requests.get",
            return_value=_mock_response(FIXTURE_HTML),
        ):
            call_command("import_brazos_tax_rates")

        harris_row = TaxUnitRate.objects.get(county="harris", tax_unit_code="G1", tax_year=2025)
        self.assertEqual(
            harris_row.adopted_rate, Decimal("0.010000").quantize(Decimal("0.00000000"))
        )
        brazos_row = TaxUnitRate.objects.get(county="brazos", tax_unit_code="G1", tax_year=2025)
        self.assertNotEqual(harris_row.adopted_rate, brazos_row.adopted_rate)

    def test_raises_when_no_table_found(self):
        with patch(
            "brazos_cad.management.commands.import_brazos_tax_rates.requests.get",
            return_value=_mock_response("<html><body>No table here.</body></html>"),
        ):
            with self.assertRaises(CommandError):
                call_command("import_brazos_tax_rates")

    def test_raises_when_year_undetectable_and_not_provided(self):
        html = FIXTURE_HTML.replace("2025 TAX RATES", "TAX RATES")
        with patch(
            "brazos_cad.management.commands.import_brazos_tax_rates.requests.get",
            return_value=_mock_response(html),
        ):
            with self.assertRaises(CommandError):
                call_command("import_brazos_tax_rates")
