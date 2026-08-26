from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import skipUnless

from django.db import connection, connections
from django.test import SimpleTestCase, TestCase

from counties.harris.etl_pipeline.config import ETLConfig
from counties.harris.etl_pipeline.persistence import (
    CopyPersistenceAdapter,
    HarrisPersistence,
    IncompletePersistenceIdentity,
    OrmPersistenceAdapter,
    PersistenceDataset,
    PersistenceRequest,
    PersistenceWriteMode,
    TranslatedRowSchemaMismatch,
    UnsafeReplacementError,
    persistence_for_connection,
)
from counties.harris.etl_pipeline.row_reader import PROPERTY_FIELD_ORDER, RowResult
from counties.harris.etl_pipeline.translated_loader import load_property_file
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord


class PropertyPersistenceContractTests(TestCase):
    def _property_row(self, account_number: str, *, address: str = "100 TEST ST") -> RowResult:
        return RowResult(
            values=(
                account_number,
                address,
                "Houston",
                "77001",
                "Owner",
                200000.0,
                190000.0,
                1800.0,
                5000.0,
                "A1",
                True,
                False,
                "100",
                "TEST",
                "",
                "",
            ),
            field_names=PROPERTY_FIELD_ORDER,
        )

    def test_replace_persists_property_and_declares_dependents_invalidated(self) -> None:
        old_property = PropertyRecord.objects.create(
            account_number="OLD001",
            address="1 OLD ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )
        BuildingDetail.objects.create(
            property=old_property,
            account_number="OLD001",
            building_number=1,
        )
        ExtraFeature.objects.create(
            property=old_property,
            account_number="OLD001",
            feature_code="POOL",
            feature_number=1,
        )

        result = HarrisPersistence(OrmPersistenceAdapter()).persist(
            PersistenceRequest(
                dataset=PersistenceDataset.PROPERTY,
                rows=iter([self._property_row("NEW001")]),
                write_mode=PersistenceWriteMode.REPLACE,
            )
        )

        self.assertEqual(result.loaded, 1)
        self.assertEqual(result.invalid, 0)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(
            result.invalidated_datasets,
            frozenset({PersistenceDataset.BUILDING, PersistenceDataset.EXTRA_FEATURE}),
        )
        self.assertTrue(PropertyRecord.objects.filter(account_number="NEW001").exists())
        self.assertFalse(PropertyRecord.objects.filter(account_number="OLD001").exists())
        self.assertEqual(BuildingDetail.objects.count(), 0)
        self.assertEqual(ExtraFeature.objects.count(), 0)

    @skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
    def test_copy_replace_matches_the_orm_contract(self) -> None:
        result = HarrisPersistence(CopyPersistenceAdapter()).persist(
            PersistenceRequest(
                dataset=PersistenceDataset.PROPERTY,
                rows=iter([self._property_row("COPY001")]),
                write_mode=PersistenceWriteMode.REPLACE,
            )
        )

        self.assertEqual(result.loaded, 1)
        self.assertEqual(result.invalid, 0)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(
            result.invalidated_datasets,
            frozenset({PersistenceDataset.BUILDING, PersistenceDataset.EXTRA_FEATURE}),
        )
        self.assertTrue(PropertyRecord.objects.filter(account_number="COPY001").exists())

    def test_production_factory_persists_property_with_the_active_adapter(self) -> None:
        result = persistence_for_connection().persist(
            PersistenceRequest(
                dataset=PersistenceDataset.PROPERTY,
                rows=iter([self._property_row("FACTORY001")]),
                write_mode=PersistenceWriteMode.REPLACE,
            )
        )

        self.assertEqual(result.loaded, 1)
        self.assertTrue(PropertyRecord.objects.filter(account_number="FACTORY001").exists())

    def test_direct_property_loader_uses_the_persistence_seam(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / "real_acct.txt"
            source.write_text(
                "acct\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val\n"
                "LOADER001\t100 MAIN ST\t77001\tA1\t250000\n",
                encoding="latin-1",
            )
            config = ETLConfig(
                download_dir=root_path / "downloads",
                extract_dir=root_path / "extracted",
                log_dir=root_path / "logs",
            )

            result = load_property_file(
                config,
                source,
                batch_size=1,
                truncate=True,
            )

        self.assertEqual(result.records_loaded, 1)
        self.assertEqual(result.records_invalid, 0)
        self.assertEqual(result.records_skipped, 0)
        self.assertTrue(PropertyRecord.objects.filter(account_number="LOADER001").exists())

    def test_add_missing_keeps_existing_properties_and_counts_duplicates_as_skipped(self) -> None:
        PropertyRecord.objects.create(
            account_number="EXISTING001",
            address="1 EXISTING ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )

        result = HarrisPersistence(OrmPersistenceAdapter(batch_size=1)).persist(
            PersistenceRequest(
                dataset=PersistenceDataset.PROPERTY,
                rows=iter(
                    [
                        self._property_row("EXISTING001", address="99 SHOULD NOT UPDATE ST"),
                        self._property_row("NEW001"),
                        self._property_row("NEW001"),
                    ]
                ),
                write_mode=PersistenceWriteMode.ADD_MISSING,
            )
        )

        self.assertEqual(result.loaded, 1)
        self.assertEqual(result.invalid, 0)
        self.assertEqual(result.skipped, 2)
        self.assertFalse(result.invalidated_datasets)
        self.assertEqual(
            PropertyRecord.objects.get(account_number="EXISTING001").address,
            "1 EXISTING ST",
        )

    @skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
    def test_copy_add_missing_matches_the_orm_duplicate_contract(self) -> None:
        PropertyRecord.objects.create(
            account_number="COPY_EXISTING",
            address="1 EXISTING ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )

        result = HarrisPersistence(CopyPersistenceAdapter()).persist(
            PersistenceRequest(
                dataset=PersistenceDataset.PROPERTY,
                rows=iter(
                    [
                        self._property_row("COPY_EXISTING", address="99 SHOULD NOT UPDATE ST"),
                        self._property_row("COPY_NEW"),
                        self._property_row("COPY_NEW"),
                    ]
                ),
                write_mode=PersistenceWriteMode.ADD_MISSING,
            )
        )

        self.assertEqual(result.loaded, 1)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(
            PropertyRecord.objects.get(account_number="COPY_EXISTING").address,
            "1 EXISTING ST",
        )

    def test_schema_mismatch_rolls_back_property_replacement(self) -> None:
        PropertyRecord.objects.create(
            account_number="KEEP_SCHEMA",
            address="1 KEEP ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )
        valid = self._property_row("BAD_SCHEMA")
        mismatched = RowResult(
            values=valid.values,
            field_names=tuple(reversed(PROPERTY_FIELD_ORDER)),
        )

        with self.assertRaises(TranslatedRowSchemaMismatch):
            HarrisPersistence(OrmPersistenceAdapter()).persist(
                PersistenceRequest(
                    dataset=PersistenceDataset.PROPERTY,
                    rows=iter([mismatched]),
                    write_mode=PersistenceWriteMode.REPLACE,
                )
            )

        self.assertTrue(PropertyRecord.objects.filter(account_number="KEEP_SCHEMA").exists())

    def test_empty_replace_rolls_back_with_source_diagnostics(self) -> None:
        PropertyRecord.objects.create(
            account_number="KEEP001",
            address="1 KEEP ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )
        skipped = RowResult(values=(), field_names=PROPERTY_FIELD_ORDER, skip=True)
        invalid = RowResult(values=(), field_names=PROPERTY_FIELD_ORDER, invalid=True)

        with self.assertRaises(UnsafeReplacementError) as raised:
            HarrisPersistence(OrmPersistenceAdapter()).persist(
                PersistenceRequest(
                    dataset=PersistenceDataset.PROPERTY,
                    rows=iter([skipped, invalid]),
                    write_mode=PersistenceWriteMode.REPLACE,
                )
            )

        self.assertEqual(raised.exception.dataset, PersistenceDataset.PROPERTY)
        self.assertEqual(raised.exception.skipped, 1)
        self.assertEqual(raised.exception.invalid, 1)
        self.assertTrue(PropertyRecord.objects.filter(account_number="KEEP001").exists())

    def test_loadable_property_without_an_account_number_is_rejected(self) -> None:
        row = self._property_row("")

        with self.assertRaises(IncompletePersistenceIdentity):
            HarrisPersistence(OrmPersistenceAdapter()).persist(
                PersistenceRequest(
                    dataset=PersistenceDataset.PROPERTY,
                    rows=iter([row]),
                    write_mode=PersistenceWriteMode.ADD_MISSING,
                )
            )


class SqlitePropertyReplacementContractTests(SimpleTestCase):
    databases = {"default", "property_persistence_sqlite"}
    database_alias = "property_persistence_sqlite"

    @classmethod
    def setUpClass(cls) -> None:
        sqlite_settings = connection.settings_dict.copy()
        sqlite_settings.update(
            {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
                "OPTIONS": {},
                "TEST": {"NAME": None},
            }
        )
        connections.databases[cls.database_alias] = sqlite_settings
        super().setUpClass()
        cls.database = connections[cls.database_alias]
        with cls.database.schema_editor() as schema_editor:
            schema_editor.create_model(PropertyRecord)
            schema_editor.create_model(BuildingDetail)
            schema_editor.create_model(ExtraFeature)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.database.close()
        del connections[cls.database_alias]
        del connections.databases[cls.database_alias]
        super().tearDownClass()

    def test_orm_replace_uses_portable_deletion_on_sqlite(self) -> None:
        PropertyRecord.objects.using(self.database_alias).create(
            account_number="SQLITE_OLD",
            address="1 OLD ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )
        row = RowResult(
            values=(
                "SQLITE_NEW",
                "100 TEST ST",
                "Houston",
                "77001",
                "Owner",
                200000.0,
                190000.0,
                1800.0,
                5000.0,
                "A1",
                True,
                False,
                "100",
                "TEST",
                "",
                "",
            ),
            field_names=PROPERTY_FIELD_ORDER,
        )

        result = HarrisPersistence(
            OrmPersistenceAdapter(database_alias=self.database_alias)
        ).persist(
            PersistenceRequest(
                dataset=PersistenceDataset.PROPERTY,
                rows=iter([row]),
                write_mode=PersistenceWriteMode.REPLACE,
            )
        )

        self.assertEqual(result.loaded, 1)
        self.assertFalse(
            PropertyRecord.objects.using(self.database_alias)
            .filter(account_number="SQLITE_OLD")
            .exists()
        )
        self.assertTrue(
            PropertyRecord.objects.using(self.database_alias)
            .filter(account_number="SQLITE_NEW")
            .exists()
        )
