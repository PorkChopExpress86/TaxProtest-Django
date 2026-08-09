"""Tests for import_hcad_jur_exempt.

The fixtures mirror the real 2025 export's shape exactly (tab-delimited, header
row, HCAD's own column names), including the two properties that motivated the
loader's design: one with no exemption, and one whose homestead + historical
exemptions stack to zero out a district.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from counties.harris.management.commands.import_hcad_jur_exempt import Command
from counties.harris.models import (
    AssessmentHistory,
    PropertyJurisdictionExemption,
    PropertyRecord,
    TaxUnitRate,
)
from counties.harris.tax_impact import calculate_tax_impact

RATE_HEADER = "RP_TYPE\ttax_dist\tname\texempt_cd\tprop\tcurr\texempt_val\texempt_rate"
VALUE_HEADER = "acct\ttax_district\ttp_cd\tpct_district\tappraised_val\ttaxable_val"
EXEMPT_HEADER = "acct\ttax_district\texempt_cat\texempt_val"
DSCR_HEADER = "exempt_cat\texemption_dscr"


class ImportHcadJurExemptTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._write_default_fixture()

        PropertyRecord.objects.create(
            address="1 Plain St",
            city="Houston",
            zipcode="77002",
            account_number="0000000000001",
            state_class="A1",
            is_residential=True,
            assessed_value=Decimal("698360"),
        )
        PropertyRecord.objects.create(
            address="2 Homestead Ave",
            city="Houston",
            zipcode="77002",
            account_number="0000000000002",
            state_class="A1",
            is_residential=True,
            assessed_value=Decimal("693903"),
        )

    # ---------------------------------------------------------------- fixtures

    def _write(self, name: str, header: str, rows: list[str]) -> None:
        (self.dir / name).write_text("\n".join([header, *rows]) + "\n", encoding="latin-1")

    def _write_default_fixture(self, *, value_rows=None, exempt_rows=None) -> None:
        # One rate row per (district, exemption code) -- the real file repeats
        # the rate and name across every exemption code for a district.
        self._write(
            "jur_tax_dist_exempt_value_rate.txt",
            RATE_HEADER,
            [
                "Real\t001\tHOUSTON ISD\tRES\t0.868300\t0.878300\t65000\t0.000000",
                "Real\t001\tHOUSTON ISD\tHIS\t0.868300\t0.878300\t0\t0.000000",
                "Real\t040\tHARRIS COUNTY\tRES\t0.370000\t0.380960\t0\t0.000000",
                "Real\t061\tCITY OF HOUSTON\tRES\t0.510000\t0.519190\t0\t0.000000",
                # Personal-property rows must be ignored.
                "Personal\t900\tPP DISTRICT\tRES\t1.000000\t2.000000\t0\t0.000000",
            ],
        )
        self._write(
            "jur_exemption_dscr.txt",
            DSCR_HEADER,
            ["RES\tResidential Homestead", "HIS\tHistorical", "TOT\tTotal"],
        )
        self._write(
            "jur_value.txt",
            VALUE_HEADER,
            (
                value_rows
                if value_rows is not None
                else [
                    # No exemptions: taxable == appraised.
                    "0000000000001\t001\tI\t1.0000\t698360\t698360",
                    "0000000000002\t040\tT\t1.0000\t698360\t698360",
                    # Homestead: taxable is net of the exemption.
                    "0000000000002\t001\tI\t1.0000\t693903\t505330",
                    # Homestead + historical stack to zero out this district.
                    "0000000000002\t061\tC\t1.0000\t693903\t0",
                ]
            ),
        )
        self._write(
            "jur_exempt.txt",
            EXEMPT_HEADER,
            (
                exempt_rows
                if exempt_rows is not None
                else [
                    "0000000000001\t001\tNONE\t",
                    "0000000000002\t040\tNONE\t",
                    "0000000000002\t001\tRES\t188573",
                    "0000000000002\t061\tRES\t48573",
                    "0000000000002\t061\tHIS\t645330",
                ]
            ),
        )

    def _run(self, *args):
        call_command("import_hcad_jur_exempt", "--tax-year", "2025", "--path", str(self.dir), *args)

    # ---------------------------------------------------------------- rates

    def test_rates_are_converted_from_per_100_dollars_to_fractional(self):
        self._run()

        houston_isd = TaxUnitRate.objects.get(tax_year=2025, county="harris", tax_unit_code="001")
        # Published as 0.878300 per $100.
        self.assertEqual(houston_isd.adopted_rate, Decimal("0.00878300"))
        self.assertEqual(houston_isd.tax_unit_name, "HOUSTON ISD")

    def test_only_real_property_districts_get_rates(self):
        self._run()

        codes = set(
            TaxUnitRate.objects.filter(county="harris").values_list("tax_unit_code", flat=True)
        )
        self.assertEqual(codes, {"001", "040", "061"})

    def test_proposed_rate_can_be_selected_instead(self):
        self._run("--rate-column", "prop")

        rate = TaxUnitRate.objects.get(tax_year=2025, tax_unit_code="001", county="harris")
        self.assertEqual(rate.adopted_rate, Decimal("0.00868300"))

    def test_rates_do_not_touch_another_county(self):
        TaxUnitRate.objects.create(
            tax_year=2025, county="brazos", tax_unit_code="001", adopted_rate=Decimal("0.01")
        )
        self._run()

        self.assertEqual(
            TaxUnitRate.objects.get(county="brazos", tax_unit_code="001").adopted_rate,
            Decimal("0.01"),
        )

    # ---------------------------------------------------------------- row shape

    def test_base_row_carries_gross_value_and_exemption_rows_carry_amounts(self):
        self._run()

        base = PropertyJurisdictionExemption.objects.get(
            account_number="0000000000002", tax_unit_code="001", exemption_code=""
        )
        # Gross, NOT HCAD's net 505330 -- calculate_tax_impact subtracts the
        # exemption itself, and would double-count against a net figure.
        self.assertEqual(base.taxable_value, Decimal("693903.00"))
        self.assertEqual(base.assessed_value, Decimal("693903.00"))
        self.assertIsNone(base.exemption_amount)
        self.assertEqual(base.tax_unit_name, "HOUSTON ISD")

        # Offset derived as gross - HCAD's net (693903 - 505330), which here
        # equals the itemised RES amount, and is coded from it.
        exemption = PropertyJurisdictionExemption.objects.get(
            account_number="0000000000002", tax_unit_code="001", exemption_code="RES"
        )
        self.assertEqual(exemption.exemption_amount, Decimal("188573.00"))
        self.assertEqual(exemption.exemption_description, "Residential Homestead")

    def test_placeholder_exemption_categories_are_not_stored(self):
        self._run()

        self.assertFalse(
            PropertyJurisdictionExemption.objects.filter(exemption_code__in=["NONE", ""])
            .exclude(exemption_code="")
            .exists()
        )
        # The blank code belongs to base rows only.
        self.assertEqual(PropertyJurisdictionExemption.objects.filter(exemption_code="").count(), 4)

    def test_duplicate_source_rows_are_deduplicated(self):
        """A repeated row must collapse to one, and must not be counted twice
        by the reconciliation either."""
        self._write_default_fixture(
            value_rows=[
                "0000000000001\t001\tI\t1.0000\t698360\t697360",
                "0000000000001\t001\tI\t1.0000\t698360\t697360",
            ],
            exempt_rows=[
                "0000000000001\t001\tRES\t1000",
                "0000000000001\t001\tRES\t1000",
            ],
        )
        self._run()

        rows = PropertyJurisdictionExemption.objects.filter(account_number="0000000000001")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.get(exemption_code="RES").exemption_amount, Decimal("1000.00"))

    def test_rerunning_replaces_rather_than_duplicates(self):
        self._run()
        first = PropertyJurisdictionExemption.objects.filter(county="harris").count()
        self._run()

        self.assertEqual(
            PropertyJurisdictionExemption.objects.filter(county="harris").count(), first
        )

    def test_other_counties_rows_survive_the_replace(self):
        PropertyJurisdictionExemption.objects.create(
            account_number="000000010002",
            tax_year=2025,
            county="brazos",
            tax_unit_code="G1",
            exemption_code="",
        )
        self._run()

        self.assertTrue(
            PropertyJurisdictionExemption.objects.filter(county="brazos", tax_year=2025).exists()
        )

    # ---------------------------------------------------------------- scoping

    def test_defaults_to_accounts_already_ingested(self):
        self._write_default_fixture(
            value_rows=[
                "0000000000001\t001\tI\t1.0000\t698360\t698360",
                "9999999999999\t001\tI\t1.0000\t100000\t100000",
            ],
            exempt_rows=[],
        )
        self._run()

        stored = set(PropertyJurisdictionExemption.objects.values_list("account_number", flat=True))
        self.assertEqual(stored, {"0000000000001"})

    def test_all_accounts_flag_keeps_unmatched_accounts(self):
        self._write_default_fixture(
            value_rows=[
                "0000000000001\t001\tI\t1.0000\t698360\t698360",
                "9999999999999\t001\tI\t1.0000\t100000\t100000",
            ],
            exempt_rows=[],
        )
        self._run("--all-accounts")

        stored = set(PropertyJurisdictionExemption.objects.values_list("account_number", flat=True))
        self.assertEqual(stored, {"0000000000001", "9999999999999"})

    # ---------------------------------------------------------------- safety

    def test_applied_offset_wins_when_itemisation_says_less(self):
        """~7k real pairs show a reduction jur_exempt.txt never itemises.

        The stored offset must come from taxable_val, or the report would show
        a tax bill higher than the one HCAD actually levies.
        """
        self._write_default_fixture(
            value_rows=["0000000000001\t001\tI\t1.0000\t455082\t159066"],
            exempt_rows=["0000000000001\t001\tNONE\t"],
        )
        self._run()

        offset = PropertyJurisdictionExemption.objects.get(
            account_number="0000000000001", tax_unit_code="001", exemption_code="EXEMPT"
        )
        self.assertEqual(offset.exemption_amount, Decimal("296016.00"))
        self.assertIn("not itemised", offset.exemption_description)

    def test_no_offset_row_when_itemisation_says_more(self):
        """~19k real pairs itemise a homestead that taxable_val does not reflect.

        Nothing was actually taken off, so nothing may be subtracted.
        """
        self._write_default_fixture(
            value_rows=["0000000000001\t001\tI\t1.0000\t303472\t303472"],
            exempt_rows=["0000000000001\t001\tRES\t84127"],
        )
        self._run()

        rows = PropertyJurisdictionExemption.objects.filter(account_number="0000000000001")
        self.assertEqual([r.exemption_code for r in rows], [""])
        self.assertEqual(rows.get().taxable_value, Decimal("303472.00"))

    def test_stacked_exemption_codes_are_named_on_the_offset_row(self):
        self._run()

        offset = PropertyJurisdictionExemption.objects.exclude(exemption_code="").get(
            account_number="0000000000002", tax_unit_code="061"
        )
        self.assertEqual(offset.exemption_code, "HIS+RES")
        # HIS 645,330 + RES 48,573 == the full 693,903 appraised value.
        self.assertEqual(offset.exemption_amount, Decimal("693903.00"))
        self.assertEqual(offset.exemption_description, "Historical + Residential Homestead")

    def test_a_failed_verification_leaves_no_rates_behind(self):
        """Rates and rows commit together, or not at all."""
        with patch.object(Command, "_verify_applied", return_value=7):
            with self.assertRaises(CommandError) as ctx:
                self._run()

        self.assertIn("Post-insert verification failed", str(ctx.exception))
        self.assertFalse(TaxUnitRate.objects.exists())
        self.assertFalse(PropertyJurisdictionExemption.objects.exists())

    def test_dry_run_writes_nothing(self):
        self._run("--dry-run")

        self.assertFalse(PropertyJurisdictionExemption.objects.exists())
        self.assertFalse(TaxUnitRate.objects.exists())

    def test_missing_directory_is_a_clear_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "import_hcad_jur_exempt", "--tax-year", "2025", "--path", "/nonexistent/dir"
            )
        self.assertIn("Extract directory not found", str(ctx.exception))

    def test_control_characters_in_source_fields_are_neutralised(self):
        """Four taxing-district names in the real 2025 export are a bare NUL byte,
        which PostgreSQL rejects outright."""
        self._write(
            "jur_tax_dist_exempt_value_rate.txt",
            RATE_HEADER,
            [
                "Real\t001\t\x00\tRES\t0.868300\t0.878300\t0\t0.000000",
                "Real\t040\tHARRIS\x00COUNTY\tRES\t0.370000\t0.380960\t0\t0.000000",
            ],
        )
        self._write(
            "jur_value.txt",
            VALUE_HEADER,
            ["0000000000001\t001\tI\t1.0000\t100000\t100000"],
        )
        self._write("jur_exempt.txt", EXEMPT_HEADER, [])
        self._run()

        self.assertEqual(
            TaxUnitRate.objects.get(tax_unit_code="001", county="harris").tax_unit_name, ""
        )
        self.assertEqual(
            TaxUnitRate.objects.get(tax_unit_code="040", county="harris").tax_unit_name,
            "HARRIS COUNTY",
        )

    def test_missing_source_file_is_a_clear_error(self):
        (self.dir / "jur_value.txt").unlink()

        with self.assertRaises(CommandError) as ctx:
            self._run()

        self.assertIn("jur_value.txt", str(ctx.exception))


class TaxImpactEndToEndTests(TestCase):
    """The point of the loader: a real Harris tax-impact figure comes out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        PropertyRecord.objects.create(
            address="2 Homestead Ave",
            city="Houston",
            zipcode="77002",
            account_number="0000000000002",
            state_class="A1",
            is_residential=True,
            assessed_value=Decimal("693903"),
        )
        AssessmentHistory.objects.create(
            account_number="0000000000002",
            tax_year=2025,
            county="harris",
            assessed_value=Decimal("693903"),
        )

        (self.dir / "jur_tax_dist_exempt_value_rate.txt").write_text(
            RATE_HEADER + "\nReal\t001\tHOUSTON ISD\tRES\t0.868300\t0.878300\t65000\t0.000000\n",
            encoding="latin-1",
        )
        (self.dir / "jur_exemption_dscr.txt").write_text(
            DSCR_HEADER + "\nRES\tResidential Homestead\n", encoding="latin-1"
        )
        (self.dir / "jur_value.txt").write_text(
            VALUE_HEADER + "\n0000000000002\t001\tI\t1.0000\t693903\t505330\n", encoding="latin-1"
        )
        (self.dir / "jur_exempt.txt").write_text(
            EXEMPT_HEADER + "\n0000000000002\t001\tRES\t188573\n", encoding="latin-1"
        )

        call_command("import_hcad_jur_exempt", "--tax-year", "2025", "--path", str(self.dir))

    def test_current_tax_matches_hcad_taxable_times_the_published_rate(self):
        result = calculate_tax_impact(
            account_number="0000000000002", tax_year=2025, median_assessed_value=None
        )

        self.assertEqual(result.completeness, "complete")
        # HCAD's own net taxable (505,330) x 0.008783 -- recomputed from gross
        # minus the exemption, not read back from a stored net figure.
        self.assertEqual(result.current_tax_owed, Decimal("4438.31"))
        self.assertEqual(result.taxable_value_used, Decimal("505330.00"))

    def test_exemptions_apply_to_the_median_scenario_too(self):
        """Savings must compare like with like: both sides net of the homestead."""
        result = calculate_tax_impact(
            account_number="0000000000002",
            tax_year=2025,
            median_assessed_value=Decimal("600000"),
        )

        # median taxable = 600,000 - 188,573 = 411,427
        self.assertEqual(result.median_tax_owed, Decimal("3613.56"))
        self.assertEqual(result.estimated_savings, result.current_tax_owed - result.median_tax_owed)
        self.assertGreater(result.estimated_savings, Decimal("0"))

    def test_the_exemption_is_reported_in_the_summary(self):
        result = calculate_tax_impact(
            account_number="0000000000002", tax_year=2025, median_assessed_value=Decimal("600000")
        )

        [applied] = result.exemptions_summary
        self.assertEqual(applied["exemption_code"], "RES")
        self.assertEqual(applied["description"], "Residential Homestead")
        self.assertEqual(applied["fixed_amount"], Decimal("188573.00"))
        self.assertEqual(applied["before_current"], Decimal("693903.00"))
        self.assertEqual(applied["after_current"], Decimal("505330.00"))
