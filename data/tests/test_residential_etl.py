from __future__ import annotations

import csv
import os
import tempfile
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from data.etl import bulk_load_properties, iter_property_rows, refresh_property_readiness
from data.models import BuildingDetail, ExtraFeature, PropertyRecord
from data.residential import is_residential_state_class


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

    def test_house_focused_residential_classes_exclude_condo_multifamily_and_auxiliary(
        self,
    ) -> None:
        self.assertTrue(is_residential_state_class("A1"))
        self.assertTrue(is_residential_state_class("A2"))
        self.assertTrue(is_residential_state_class("A4"))
        self.assertTrue(is_residential_state_class("E1"))
        self.assertFalse(is_residential_state_class("A3"))
        self.assertFalse(is_residential_state_class("B1"))
        self.assertFalse(is_residential_state_class("B2"))
        self.assertFalse(is_residential_state_class("Z1"))
        self.assertFalse(is_residential_state_class("Z4"))

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

    def test_bulk_load_properties_excludes_condo_and_multifamily_rows(self) -> None:
        filepath = self._create_real_acct_file(
            [
                "111\t111 MAIN ST\tHOUSTON\t77001\tA1\t250000\t2000\t8000\tOWNER ONE\t111\tMAIN",
                "222\t222 HIGHRISE ST\tHOUSTON\t77002\tZ4\t350000\t1200\t1000\tOWNER TWO\t222\tHIGHRISE",
                "333\t333 APARTMENT AVE\tHOUSTON\t77003\tB1\t450000\t5000\t12000\tOWNER THREE\t333\tAPARTMENT",
                "444\t444 GARAGE LN\tHOUSTON\t77004\tA3\t150000\t0\t4000\tOWNER FOUR\t444\tGARAGE",
            ]
        )

        inserted = bulk_load_properties(filepath, chunk_size=10, truncate=True)

        self.assertEqual(inserted, 1)
        self.assertEqual(
            list(PropertyRecord.objects.values_list("account_number", flat=True)),
            ["111"],
        )

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

    @patch("data.management.commands.import_all_data.ETLOrchestrator.execute")
    def test_import_all_data_fails_when_authoritative_pipeline_fails(self, mocked_execute) -> None:
        mocked_execute.return_value = SimpleNamespace(
            success=False,
            status=SimpleNamespace(value="failed"),
            duration=0.1,
            stages={},
            errors=["validation failed"],
        )

        with self.assertRaises(CommandError):
            call_command("import_all_data", skip_download=True, skip_property=True)

    @patch("data.management.commands.import_all_data.ETLOrchestrator.execute")
    def test_import_all_data_delegates_to_modern_pipeline_with_strict_mode(
        self, mocked_execute
    ) -> None:
        mocked_execute.return_value = SimpleNamespace(
            success=True,
            status=SimpleNamespace(value="completed"),
            duration=0.1,
            stages={},
            errors=[],
        )

        call_command("import_all_data", skip_download=True, skip_property=True)

        mocked_execute.assert_called_once()
        _, kwargs = mocked_execute.call_args
        self.assertEqual(kwargs["scope"], "full")
        self.assertTrue(kwargs["strict"])
        self.assertTrue(kwargs["validate_contract"])
        self.assertTrue(kwargs["skip_download"])
        self.assertTrue(kwargs["skip_extract"])

    @patch("data.management.commands.import_all_data.ETLOrchestrator.execute")
    def test_import_all_data_uses_property_only_scope_when_gis_is_skipped(
        self, mocked_execute
    ) -> None:
        mocked_execute.return_value = SimpleNamespace(
            success=True,
            status=SimpleNamespace(value="completed"),
            duration=0.1,
            stages={},
            errors=[],
        )

        call_command("import_all_data", skip_download=True, skip_gis=True)

        mocked_execute.assert_called_once()
        _, kwargs = mocked_execute.call_args
        self.assertEqual(kwargs["scope"], "property-only")

    @patch("data.management.commands.import_all_data.ETLOrchestrator.execute")
    def test_import_all_data_can_skip_contract_validation_for_startup_refresh(
        self, mocked_execute
    ) -> None:
        mocked_execute.return_value = SimpleNamespace(
            success=True,
            status=SimpleNamespace(value="completed"),
            duration=0.1,
            stages={},
            errors=[],
        )

        call_command(
            "import_all_data",
            skip_download=True,
            skip_property=True,
            skip_contract_validation=True,
        )

        mocked_execute.assert_called_once()
        _, kwargs = mocked_execute.call_args
        self.assertFalse(kwargs["validate_contract"])


class ETLLoaderOptimizationTests(TestCase):
    def _create_temp_file(self, header: str, rows: list[str]) -> str:
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        handle.write(header + "\n")
        for row in rows:
            handle.write(row + "\n")
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_load_building_details_uses_cached_property_map(self) -> None:
        from data.etl import load_building_details

        prop = PropertyRecord.objects.create(
            address="1 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="ACC1",
            state_class="A1",
            is_residential=True,
        )
        PropertyRecord.objects.create(
            address="9 COMMERCE ST",
            city="Houston",
            zipcode="77002",
            account_number="ACC9",
            state_class="F1",
            is_residential=False,
        )
        path = self._create_temp_file(
            "acct\tbld_num\timprv_type\tqa_cd\tcndtn_cd\tdate_erected\theat_ar",
            [
                "ACC1\t1\tA1\tC\tAV\t2001\t1500",
                "ACC9\t1\tA1\tC\tAV\t2002\t1300",
                "MISSING\t1\tA1\tC\tAV\t2002\t1300",
                "\t1\tA1\tC\tAV\t2003\t1100",
            ],
        )

        result = load_building_details(path, chunk_size=50, import_batch_id="b1")

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["invalid"], 2)
        self.assertEqual(result["skipped"], 1)
        building = BuildingDetail.objects.get(account_number="ACC1")
        self.assertEqual(building.property_id, prop.id)

    def test_load_extra_features_uses_cached_property_map(self) -> None:
        from data.etl import load_extra_features

        prop = PropertyRecord.objects.create(
            address="2 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="ACC2",
            state_class="A1",
            is_residential=True,
        )
        PropertyRecord.objects.create(
            address="8 COMMERCE ST",
            city="Houston",
            zipcode="77002",
            account_number="ACC8",
            state_class="F1",
            is_residential=False,
        )
        path = self._create_temp_file(
            "acct\tbld_num\tcd\tdscr\tunits\tlength\twidth\tgrade\tcond_cd\tact_yr\tasd_val",
            [
                "ACC2\t1\tPOOL\tPool\t1\t20\t10\tA\tG\t2018\t5000",
                "ACC8\t1\tGAR\tGarage\t1\t10\t10\tA\tG\t2010\t3000",
                "MISSING\t1\tGAR\tGarage\t1\t10\t10\tA\tG\t2010\t3000",
                "\t1\tPOR\tPorch\t1\t8\t8\tB\tF\t2015\t1500",
            ],
        )

        result = load_extra_features(path, chunk_size=50, import_batch_id="b2", truncate=True)

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["invalid"], 2)
        self.assertEqual(result["skipped"], 1)
        feature = ExtraFeature.objects.get(account_number="ACC2")
        self.assertEqual(feature.property_id, prop.id)
        self.assertEqual(feature.feature_description, "Pool")
        self.assertEqual(feature.quantity, Decimal("1"))
        self.assertEqual(feature.length, Decimal("20"))
        self.assertEqual(feature.width, Decimal("10"))
        self.assertEqual(feature.condition_code, "G")
        self.assertEqual(feature.year_built, 2018)
        self.assertEqual(feature.value, Decimal("5000"))

    def test_load_extra_features_supports_fallback_long_description_file(self) -> None:
        from data.etl import load_extra_features

        PropertyRecord.objects.create(
            address="4 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="ACC4",
            state_class="A1",
            is_residential=True,
        )
        path = self._create_temp_file(
            "acct\tbld_num\tcount\tgrade\tcd\ts_dscr\tl_dscr\tcat\tdscr\tnote\tuts",
            [
                "ACC4\t0\t1\t4\tCPA1\tPavAsp\tPaving - Asphalt\tMS\tMiscellaneous\t\t5000.00",
            ],
        )

        result = load_extra_features(path, chunk_size=50, import_batch_id="b3", truncate=True)

        self.assertEqual(result["imported"], 1)
        feature = ExtraFeature.objects.get(account_number="ACC4")
        self.assertEqual(feature.feature_description, "Paving - Asphalt")
        self.assertEqual(feature.quantity, Decimal("1"))
        self.assertEqual(feature.value, Decimal("5000"))

    def test_load_fixtures_room_counts_bulk_updates_and_not_found_tracking(self) -> None:
        from data.etl import load_fixtures_room_counts

        prop = PropertyRecord.objects.create(
            address="3 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="ACC3",
            state_class="A1",
            is_residential=True,
        )
        b1 = BuildingDetail.objects.create(
            property=prop,
            account_number="ACC3",
            building_number=1,
            is_active=True,
        )
        b2 = BuildingDetail.objects.create(
            property=prop,
            account_number="ACC3",
            building_number=2,
            is_active=True,
        )
        path = self._create_temp_file(
            "acct\tbld_num\ttype\ttype_dscr\tunits",
            [
                "ACC3\t1\tRMB\tRoom: Bedroom\t4.00",
                "ACC3\t1\tRMF\tRoom: Full Bath\t2.00",
                "ACC3\t1\tRMH\tRoom: Half Bath\t1.00",
                "ACC3\t2\tRMB\tRoom: Bedroom\t3.00",
                "ACC3\t2\tRMF\tRoom: Full Bath\t1.00",
                "NOPE\t1\tRMB\tRoom: Bedroom\t2.00",
            ],
        )

        result = load_fixtures_room_counts(path, chunk_size=2, refresh_readiness=False)

        b1.refresh_from_db()
        b2.refresh_from_db()
        self.assertEqual(result["buildings_updated"], 2)
        self.assertEqual(result["buildings_not_found"], 1)
        self.assertEqual(b1.bedrooms, 4)
        self.assertEqual(b1.bathrooms, Decimal("2.5"))
        self.assertEqual(b1.half_baths, 1)
        self.assertEqual(b2.bedrooms, 3)
        self.assertEqual(b2.bathrooms, Decimal("1"))

    @patch("data.etl.gpd.read_file")
    @patch("data.etl.GEOPANDAS_AVAILABLE", True)
    def test_load_gis_parcels_updates_records_with_account_map(self, mocked_read_file) -> None:
        from data.etl import load_gis_parcels

        prop1 = PropertyRecord.objects.create(
            address="4 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="GIS1",
            state_class="A1",
            is_residential=True,
        )
        prop2 = PropertyRecord.objects.create(
            address="5 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="GIS2",
            state_class="A1",
            is_residential=True,
        )
        non_res = PropertyRecord.objects.create(
            address="6 COMMERCE ST",
            city="Houston",
            zipcode="77002",
            account_number="GIS_NON",
            state_class="F1",
            is_residential=False,
        )

        class _FakeCentroid:
            def __init__(self, rows):
                self.x = [row["x"] for row in rows]
                self.y = [row["y"] for row in rows]

        class _FakeGeometry:
            def __init__(self, rows):
                self.centroid = _FakeCentroid(rows)

        class _FakeCRS:
            def to_epsg(self):
                return 4326

        class _FakeGDF:
            def __init__(self, rows):
                self._rows = rows
                self.columns = ["ACCT", "PARCEL_ID"]
                self.crs = _FakeCRS()
                self.geometry = _FakeGeometry(rows)
                self._derived = {}

            def __len__(self):
                return len(self._rows)

            def __setitem__(self, key, value):
                self._derived[key] = value
                if key in ("latitude", "longitude"):
                    for row, v in zip(self._rows, value):
                        row[key] = v

            def __getitem__(self, key):
                if key == "centroid":
                    return self._derived.get("centroid", self.geometry.centroid)
                return self._derived.get(key)

            def to_crs(self, epsg):
                return self

            def itertuples(self, index=False):
                for row in self._rows:
                    yield SimpleNamespace(
                        ACCT=row["ACCT"],
                        PARCEL_ID=row["PARCEL_ID"],
                        latitude=row.get("latitude"),
                        longitude=row.get("longitude"),
                    )

        mocked_read_file.return_value = _FakeGDF(
            [
                {"ACCT": "GIS1", "PARCEL_ID": "P1", "x": -95.1, "y": 29.1},
                {"ACCT": "GIS2", "PARCEL_ID": "P2", "x": -95.2, "y": 29.2},
                {"ACCT": "GIS2", "PARCEL_ID": "P2B", "x": -95.25, "y": 29.25},
                {"ACCT": "GIS_NON", "PARCEL_ID": "PNR", "x": -95.26, "y": 29.26},
                {"ACCT": "MISSING", "PARCEL_ID": "P3", "x": -95.3, "y": 29.3},
                {"ACCT": "", "PARCEL_ID": "P4", "x": -95.4, "y": 29.4},
            ]
        )

        updated = load_gis_parcels("fake.shp", chunk_size=2, refresh_readiness=False)

        self.assertEqual(updated, 2)
        prop1.refresh_from_db()
        prop2.refresh_from_db()
        non_res.refresh_from_db()
        self.assertEqual(prop1.parcel_id, "P1")
        self.assertEqual(prop2.parcel_id, "P2B")
        self.assertIsNone(non_res.latitude)
        self.assertIsNone(non_res.longitude)
        self.assertNotEqual(non_res.parcel_id, "PNR")


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
