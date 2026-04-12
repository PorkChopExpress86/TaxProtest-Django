from __future__ import annotations

import csv
import os
import tempfile
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from data.etl import bulk_load_properties, iter_property_rows, refresh_property_readiness
from data.management.commands.import_all_data import Command as ImportAllDataCommand
from data.models import BuildingDetail, PropertyRecord


class ResidentialPropertyImportTests(TestCase):
    def _create_real_acct_file(self, rows: list[str]) -> str:
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        handle.write(
            "acct\tsite_addr_1\tsite_addr_2\tsite_addr_3\tstate_class\ttot_appr_val\tbld_ar\tland_ar\tmailto\tstr_num\tstr\n"
        )
        for row in rows:
            handle.write(row + "\n")
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_iter_property_rows_marks_residential_state_class(self) -> None:
        reader = csv.DictReader(
            StringIO(
                "acct\tsite_addr_1\tsite_addr_2\tsite_addr_3\tstate_class\ttot_appr_val\tbld_ar\tland_ar\tmailto\tstr_num\tstr\n"
                "111\t111 MAIN ST\tHOUSTON\t77001\tA1\t250000\t2000\t8000\tOWNER ONE\t111\tMAIN\n"
                "222\t222 COMMERCE ST\tHOUSTON\t77002\tF1\t450000\t5000\t12000\tOWNER TWO\t222\tCOMMERCE\n"
            ),
            delimiter="\t",
        )

        rows = list(iter_property_rows(reader))

        self.assertEqual(rows[0]["state_class"], "A1")
        self.assertTrue(rows[0]["is_residential"])
        self.assertEqual(rows[1]["state_class"], "F1")
        self.assertFalse(rows[1]["is_residential"])

    def test_bulk_load_properties_filters_non_residential_rows(self) -> None:
        filepath = self._create_real_acct_file(
            [
                "111\t111 MAIN ST\tHOUSTON\t77001\tA1\t250000\t2000\t8000\tOWNER ONE\t111\tMAIN",
                "222\t222 COMMERCE ST\tHOUSTON\t77002\tF1\t450000\t5000\t12000\tOWNER TWO\t222\tCOMMERCE",
            ]
        )

        inserted = bulk_load_properties(filepath, chunk_size=10, truncate=True)

        self.assertEqual(inserted, 1)
        self.assertEqual(PropertyRecord.objects.count(), 1)

        prop = PropertyRecord.objects.get(account_number="111")
        self.assertEqual(prop.state_class, "A1")
        self.assertTrue(prop.is_residential)
        self.assertFalse(prop.is_data_ready)

    def test_refresh_property_readiness_requires_rooms_building_and_gis(self) -> None:
        ready_prop = PropertyRecord.objects.create(
            address="111 READY ST",
            city="Houston",
            zipcode="77001",
            value=Decimal("250000"),
            account_number="READY001",
            owner_name="Ready Owner",
            assessed_value=Decimal("245000"),
            building_area=Decimal("2000"),
            land_area=Decimal("8000"),
            state_class="A1",
            is_residential=True,
            latitude=Decimal("29.7600000"),
            longitude=Decimal("-95.3700000"),
        )
        BuildingDetail.objects.create(
            property=ready_prop,
            account_number=ready_prop.account_number,
            building_number=1,
            quality_code="C",
            condition_code="AV",
            year_built=2005,
            heat_area=Decimal("2000"),
            bedrooms=3,
            bathrooms=Decimal("2.0"),
            is_active=True,
        )

        missing_gis_prop = PropertyRecord.objects.create(
            address="222 WAITING ST",
            city="Houston",
            zipcode="77001",
            value=Decimal("275000"),
            account_number="WAIT001",
            owner_name="Waiting Owner",
            assessed_value=Decimal("270000"),
            building_area=Decimal("2100"),
            land_area=Decimal("8500"),
            state_class="A1",
            is_residential=True,
        )
        BuildingDetail.objects.create(
            property=missing_gis_prop,
            account_number=missing_gis_prop.account_number,
            building_number=1,
            quality_code="C",
            condition_code="AV",
            year_built=2005,
            heat_area=Decimal("2100"),
            bedrooms=3,
            bathrooms=Decimal("2.0"),
            is_active=True,
        )

        non_residential = PropertyRecord.objects.create(
            address="333 OFFICE ST",
            city="Houston",
            zipcode="77002",
            value=Decimal("450000"),
            account_number="OFFICE001",
            owner_name="Office Owner",
            assessed_value=Decimal("430000"),
            building_area=Decimal("5000"),
            land_area=Decimal("12000"),
            state_class="F1",
            is_residential=False,
            latitude=Decimal("29.7610000"),
            longitude=Decimal("-95.3710000"),
        )
        BuildingDetail.objects.create(
            property=non_residential,
            account_number=non_residential.account_number,
            building_number=1,
            quality_code="C",
            condition_code="AV",
            year_built=2005,
            heat_area=Decimal("5000"),
            bedrooms=10,
            bathrooms=Decimal("4.0"),
            is_active=True,
        )

        results = refresh_property_readiness()

        ready_prop = PropertyRecord.objects.get(pk=ready_prop.pk)
        missing_gis_prop = PropertyRecord.objects.get(pk=missing_gis_prop.pk)
        non_residential = PropertyRecord.objects.get(pk=non_residential.pk)

        self.assertEqual(results["ready_properties_set"], 1)
        self.assertTrue(ready_prop.is_data_ready)
        self.assertFalse(missing_gis_prop.is_data_ready)
        self.assertFalse(non_residential.is_data_ready)


class ResidentialValidationCommandTests(TestCase):
    def _create_ready_property(self, account_number: str = "VALID001") -> PropertyRecord:
        prop = PropertyRecord.objects.create(
            address="100 VALID ST",
            city="Houston",
            zipcode="77001",
            value=Decimal("250000"),
            account_number=account_number,
            owner_name="Valid Owner",
            assessed_value=Decimal("245000"),
            building_area=Decimal("2000"),
            land_area=Decimal("8000"),
            state_class="A1",
            is_residential=True,
            latitude=Decimal("29.7600000"),
            longitude=Decimal("-95.3700000"),
        )
        BuildingDetail.objects.create(
            property=prop,
            account_number=account_number,
            building_number=1,
            quality_code="C",
            condition_code="AV",
            year_built=2005,
            heat_area=Decimal("2000"),
            bedrooms=3,
            bathrooms=Decimal("2.0"),
            is_active=True,
        )
        refresh_property_readiness()
        return prop

    def test_validate_data_passes_for_residential_ready_dataset(self) -> None:
        self._create_ready_property()

        call_command("validate_data")

    def test_validate_data_fails_when_non_residential_property_exists(self) -> None:
        self._create_ready_property()
        PropertyRecord.objects.create(
            address="200 COMMERCIAL ST",
            city="Houston",
            zipcode="77002",
            value=Decimal("500000"),
            account_number="NONRES001",
            owner_name="Commercial Owner",
            assessed_value=Decimal("480000"),
            building_area=Decimal("5000"),
            land_area=Decimal("12000"),
            state_class="F1",
            is_residential=False,
            latitude=Decimal("29.7610000"),
            longitude=Decimal("-95.3710000"),
        )

        with self.assertRaises(CommandError):
            call_command("validate_data")

    def test_validate_data_can_skip_gis_checks(self) -> None:
        prop = self._create_ready_property(account_number="NOGIS001")
        prop.latitude = None
        prop.longitude = None
        prop.save(update_fields=["latitude", "longitude"])
        refresh_property_readiness()

        call_command("validate_data", skip_gis_checks=True)


class ImportAllDataCommandTests(TestCase):
    def _create_property(
        self,
        *,
        account_number: str,
        state_class: str = "A1",
        is_residential: bool = True,
        with_building: bool = True,
        with_rooms: bool = True,
        with_gis: bool = True,
    ) -> PropertyRecord:
        prop = PropertyRecord.objects.create(
            address=f"{account_number} TEST ST",
            city="Houston",
            zipcode="77001",
            value=Decimal("250000"),
            account_number=account_number,
            owner_name="Test Owner",
            assessed_value=Decimal("245000"),
            building_area=Decimal("2000"),
            land_area=Decimal("8000"),
            state_class=state_class,
            is_residential=is_residential,
            latitude=Decimal("29.7600000") if with_gis else None,
            longitude=Decimal("-95.3700000") if with_gis else None,
        )

        if with_building:
            BuildingDetail.objects.create(
                property=prop,
                account_number=account_number,
                building_number=1,
                quality_code="C",
                condition_code="AV",
                year_built=2005,
                heat_area=Decimal("2000"),
                bedrooms=3 if with_rooms else None,
                bathrooms=Decimal("2.0") if with_rooms else None,
                is_active=True,
            )

        refresh_property_readiness()
        return prop

    @patch.object(ImportAllDataCommand, "run_stage_command", autospec=True)
    def test_import_all_data_fails_when_requested_completeness_is_missing(self, mocked_run_stage) -> None:
        self._create_property(
            account_number="INCOMPLETE001",
            with_building=True,
            with_rooms=True,
            with_gis=False,
        )

        with self.assertRaises(CommandError):
            call_command("import_all_data", skip_download=True, skip_property=True)

        self.assertEqual(mocked_run_stage.call_count, 2)

    @patch.object(ImportAllDataCommand, "run_stage_command", autospec=True)
    def test_import_all_data_succeeds_when_existing_dataset_is_ready(self, mocked_run_stage) -> None:
        self._create_property(account_number="READYIMPORT001")

        call_command("import_all_data", skip_download=True, skip_property=True)

        self.assertEqual(mocked_run_stage.call_count, 2)


class ReconcilePropertyDataCommandTests(TestCase):
    def _create_property(
        self,
        *,
        account_number: str,
        state_class: str,
        is_residential: bool,
        with_building: bool = True,
        with_rooms: bool = True,
        with_gis: bool = True,
    ) -> PropertyRecord:
        prop = PropertyRecord.objects.create(
            address=f"{account_number} TEST ST",
            city="Houston",
            zipcode="77001",
            value=Decimal("250000"),
            account_number=account_number,
            owner_name="Legacy Owner",
            assessed_value=Decimal("245000"),
            building_area=Decimal("2000"),
            land_area=Decimal("8000"),
            state_class=state_class,
            is_residential=is_residential,
            latitude=Decimal("29.7600000") if with_gis else None,
            longitude=Decimal("-95.3700000") if with_gis else None,
        )

        if with_building:
            BuildingDetail.objects.create(
                property=prop,
                account_number=account_number,
                building_number=1,
                quality_code="C",
                condition_code="AV",
                year_built=2005,
                heat_area=Decimal("2000"),
                bedrooms=3 if with_rooms else None,
                bathrooms=Decimal("2.0") if with_rooms else None,
                is_active=True,
            )

        refresh_property_readiness()
        return prop

    def test_reconcile_property_data_dry_run_is_non_destructive(self) -> None:
        self._create_property(account_number="KEEP001", state_class=" a1 ", is_residential=False)
        self._create_property(account_number="DROP001", state_class="F1", is_residential=False)
        self._create_property(
            account_number="DROP002",
            state_class="A1",
            is_residential=True,
            with_gis=False,
        )

        call_command("reconcile_property_data")

        self.assertEqual(PropertyRecord.objects.count(), 3)
        self.assertTrue(PropertyRecord.objects.filter(account_number="KEEP001").exists())
        self.assertTrue(PropertyRecord.objects.filter(account_number="DROP001").exists())
        self.assertTrue(PropertyRecord.objects.filter(account_number="DROP002").exists())

    def test_reconcile_property_data_apply_removes_legacy_rows_and_keeps_ready_ones(self) -> None:
        self._create_property(account_number="KEEP001", state_class=" a1 ", is_residential=False)
        self._create_property(account_number="DROP001", state_class="F1", is_residential=False)
        self._create_property(
            account_number="DROP002",
            state_class="A1",
            is_residential=True,
            with_gis=False,
        )

        call_command("reconcile_property_data", apply=True)

        self.assertEqual(PropertyRecord.objects.count(), 1)
        kept = PropertyRecord.objects.get(account_number="KEEP001")
        self.assertEqual(kept.state_class, "A1")
        self.assertTrue(kept.is_residential)
        self.assertTrue(kept.is_data_ready)
        self.assertFalse(PropertyRecord.objects.filter(account_number="DROP001").exists())
        self.assertFalse(PropertyRecord.objects.filter(account_number="DROP002").exists())
