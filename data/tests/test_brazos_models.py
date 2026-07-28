"""Tests for the PropertyAccount value accessors.

The export names two fields `appraised_val` and `assessed_val` that are computed
*before* the agricultural productivity deduction. Reading them directly overstates
ag land by the deduction — often tenfold. These accessors are the guardrail, so
they are tested against the real figures Brazos CAD publishes.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from data.models import BrazosImportRun, PropertyAccount


class ValueAccessorTests(TestCase):
    def test_ag_property_reports_post_deduction_values(self):
        # Property 10002, 2025: a 573.9-acre ranch under ag valuation. The
        # district publishes market 2,865,095 but appraised/assessed 136,442.
        account = PropertyAccount.objects.create(
            prop_id=10002,
            tax_year=2025,
            ag_market=Decimal("2833677"),
            ag_use_val=Decimal("105024"),
            imprv_non_hstd_val=Decimal("31418"),
            appraised_val=Decimal("2865095"),
            assessed_val=Decimal("2865095"),
            appraised_val_prod_loss=Decimal("136442"),
            assessed_val_prod_loss=Decimal("136442"),
        )
        self.assertEqual(account.market_value, Decimal("2865095"))
        self.assertEqual(account.appraised_value, Decimal("136442"))
        self.assertEqual(account.assessed_value, Decimal("136442"))
        self.assertEqual(account.ag_value_loss, Decimal("2728653"))
        # The trap this guards against.
        self.assertNotEqual(account.appraised_value, account.appraised_val)

    def test_non_ag_property_collapses_to_one_number(self):
        account = PropertyAccount.objects.create(
            prop_id=349514,
            tax_year=2025,
            land_hstd_val=Decimal("72000"),
            imprv_hstd_val=Decimal("291315"),
            appraised_val=Decimal("363315"),
            assessed_val=Decimal("363315"),
            appraised_val_prod_loss=Decimal("363315"),
            assessed_val_prod_loss=Decimal("363315"),
        )
        self.assertEqual(account.market_value, account.appraised_value)
        self.assertEqual(account.appraised_value, account.assessed_value)
        self.assertEqual(account.ag_value_loss, Decimal("0"))

    def test_circuit_breaker_is_reflected_in_assessed_value(self):
        # Property 38698, 2025: 678,000 appraised, 89,449 SB2 limitation.
        account = PropertyAccount.objects.create(
            prop_id=38698,
            tax_year=2025,
            appraised_val=Decimal("678000"),
            assessed_val=Decimal("588551"),
            appraised_val_prod_loss=Decimal("678000"),
            assessed_val_prod_loss=Decimal("588551"),
            circuit_breaker_val=Decimal("89449"),
        )
        self.assertEqual(account.market_value, Decimal("678000"))
        self.assertEqual(account.assessed_value, Decimal("588551"))
        self.assertEqual(
            account.appraised_value - account.circuit_breaker_val, account.assessed_value
        )

    def test_falls_back_when_prod_loss_columns_are_absent(self):
        # An export predating those fields leaves them NULL; the accessors must
        # degrade to the raw values rather than returning None.
        account = PropertyAccount.objects.create(
            prop_id=1,
            tax_year=2019,
            appraised_val=Decimal("100000"),
            assessed_val=Decimal("95000"),
        )
        self.assertEqual(account.appraised_value, Decimal("100000"))
        self.assertEqual(account.assessed_value, Decimal("95000"))


class SitusAddressTests(TestCase):
    def make(self, **kwargs) -> PropertyAccount:
        return PropertyAccount(prop_id=1, tax_year=2025, **kwargs)

    def test_builds_full_address(self):
        account = self.make(situs_num="4220", situs_street="ROCK BEND", situs_street_suffix="DR")
        self.assertEqual(account.situs_address, "4220 ROCK BEND DR")

    def test_appends_unit_without_a_hash(self):
        # Matches how the district prints it: "1640 BRIARCREST DR 100".
        account = self.make(
            situs_num="1640",
            situs_street="BRIARCREST",
            situs_street_suffix="DR",
            situs_unit="100",
        )
        self.assertEqual(account.situs_address, "1640 BRIARCREST DR 100")

    def test_includes_prefix(self):
        account = self.make(situs_num="12", situs_street_prefix="N", situs_street="MAIN")
        self.assertEqual(account.situs_address, "12 N MAIN")

    def test_street_without_number_still_renders(self):
        # 43% of rows carry a street but no number.
        account = self.make(situs_street="SILVER HILL", situs_street_suffix="RD")
        self.assertEqual(account.situs_address, "SILVER HILL RD")

    def test_empty_when_no_situs_data(self):
        self.assertEqual(self.make().situs_address, "")


class TaxingUnitTests(TestCase):
    def test_splits_entity_list(self):
        account = PropertyAccount(prop_id=1, tax_year=2025, entities="C2, CAD, G1, S2, ZRFND")
        self.assertEqual(account.taxing_units, ["C2", "CAD", "G1", "S2", "ZRFND"])

    def test_empty_entities_gives_empty_list(self):
        self.assertEqual(PropertyAccount(prop_id=1, tax_year=2025).taxing_units, [])


class ImportRunProvenanceTests(TestCase):
    def test_certified_roll_is_supplement_zero(self):
        run = BrazosImportRun.objects.create(tax_year=2025, export_supplement_num=0)
        self.assertTrue(run.is_certified_roll)

    def test_supplemented_export_is_not_the_certified_roll(self):
        run = BrazosImportRun.objects.create(tax_year=2025, export_supplement_num=3)
        self.assertFalse(run.is_certified_roll)

    def test_unknown_supplement_is_not_assumed_certified(self):
        run = BrazosImportRun.objects.create(tax_year=2025)
        self.assertFalse(run.is_certified_roll)
