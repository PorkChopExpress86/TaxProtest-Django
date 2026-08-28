"""Behavioral contract tests for Building through the Harris persistence seam."""

from __future__ import annotations

from unittest import skipUnless

from django.db import connection
from django.test import TestCase

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
from counties.harris.etl_pipeline.row_reader import BUILDING_FIELD_ORDER, RowResult
from counties.harris.models import BuildingDetail, PropertyRecord


class BuildingPersistenceContractTests(TestCase):
    def setUp(self) -> None:
        self.property_record = PropertyRecord.objects.create(
            account_number="PROP001",
            address="1 TEST ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )

    def _building_row(
        self,
        account_number: str = "PROP001",
        building_number: int | None = 1,
        *,
        building_type: str = "A1",
    ) -> RowResult:
        return RowResult(
            values=(
                self.property_record.pk,
                account_number,
                building_number,
                building_type,
                "",
                "R3",
                "A",
                "G",
                1995,
                None,
                None,
                1800.0,
                None,
                None,
                1.0,
                "",
                "",
                "",
                "",
                3,
                2.0,
                1,
                0,
                True,
            ),
            field_names=BUILDING_FIELD_ORDER,
        )

    def _persist(
        self,
        adapter: OrmPersistenceAdapter | CopyPersistenceAdapter,
        rows: list[RowResult],
        *,
        write_mode: PersistenceWriteMode,
    ):
        return HarrisPersistence(adapter).persist(
            PersistenceRequest(
                dataset=PersistenceDataset.BUILDING,
                rows=iter(rows),
                write_mode=write_mode,
            )
        )

    def test_orm_replace_persists_building_with_call_metadata(self) -> None:
        result = self._persist(
            OrmPersistenceAdapter(),
            [self._building_row()],
            write_mode=PersistenceWriteMode.REPLACE,
        )

        building = BuildingDetail.objects.get(account_number="PROP001", building_number=1)
        self.assertEqual(result.dataset, PersistenceDataset.BUILDING)
        self.assertIs(result.write_mode, PersistenceWriteMode.REPLACE)
        self.assertEqual((result.loaded, result.invalid, result.skipped), (1, 0, 0))
        self.assertFalse(result.invalidated_datasets)
        self.assertEqual(building.import_batch_id, result.batch_id)
        self.assertIsNotNone(building.import_date)
        self.assertEqual(building.created_at, building.updated_at)

    @skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
    def test_copy_replace_matches_orm_state_and_metadata_contract(self) -> None:
        copy_result = self._persist(
            CopyPersistenceAdapter(),
            [self._building_row()],
            write_mode=PersistenceWriteMode.REPLACE,
        )
        copy_building = BuildingDetail.objects.get(account_number="PROP001", building_number=1)
        copy_snapshot = (
            copy_building.property_id,
            copy_building.account_number,
            copy_building.building_number,
            copy_building.building_type,
            copy_building.bedrooms,
            copy_building.bathrooms,
            copy_building.import_batch_id,
        )

        orm_result = self._persist(
            OrmPersistenceAdapter(),
            [self._building_row()],
            write_mode=PersistenceWriteMode.REPLACE,
        )
        orm_building = BuildingDetail.objects.get(account_number="PROP001", building_number=1)
        orm_snapshot = (
            orm_building.property_id,
            orm_building.account_number,
            orm_building.building_number,
            orm_building.building_type,
            orm_building.bedrooms,
            orm_building.bathrooms,
            orm_building.import_batch_id,
        )

        self.assertEqual((copy_result.loaded, copy_result.invalid, copy_result.skipped), (1, 0, 0))
        self.assertEqual((orm_result.loaded, orm_result.invalid, orm_result.skipped), (1, 0, 0))
        self.assertEqual(copy_snapshot[:-1], orm_snapshot[:-1])
        self.assertEqual(copy_snapshot[-1], copy_result.batch_id)
        self.assertEqual(orm_snapshot[-1], orm_result.batch_id)

    def test_add_missing_skips_complete_duplicates_and_existing_conflicts(self) -> None:
        BuildingDetail.objects.create(
            property=self.property_record,
            account_number="PROP001",
            building_number=1,
            building_type="ORIGINAL",
            import_batch_id="existing-batch",
        )

        result = self._persist(
            OrmPersistenceAdapter(batch_size=1),
            [
                self._building_row(building_type="NOUPDATE"),
                self._building_row(building_number=2),
                self._building_row(building_number=2),
            ],
            write_mode=PersistenceWriteMode.ADD_MISSING,
        )

        existing = BuildingDetail.objects.get(account_number="PROP001", building_number=1)
        self.assertEqual((result.loaded, result.invalid, result.skipped), (1, 0, 2))
        self.assertEqual(existing.building_type, "ORIGINAL")
        self.assertEqual(existing.import_batch_id, "existing-batch")
        self.assertTrue(
            BuildingDetail.objects.filter(account_number="PROP001", building_number=2).exists()
        )

    @skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
    def test_copy_add_missing_matches_orm_duplicate_contract(self) -> None:
        BuildingDetail.objects.create(
            property=self.property_record,
            account_number="PROP001",
            building_number=1,
            building_type="ORIGINAL",
            import_batch_id="existing-batch",
        )

        result = self._persist(
            CopyPersistenceAdapter(),
            [
                self._building_row(building_type="NOUPDATE"),
                self._building_row(building_number=2),
                self._building_row(building_number=2),
            ],
            write_mode=PersistenceWriteMode.ADD_MISSING,
        )

        existing = BuildingDetail.objects.get(account_number="PROP001", building_number=1)
        self.assertEqual((result.loaded, result.invalid, result.skipped), (1, 0, 2))
        self.assertEqual(existing.building_type, "ORIGINAL")
        self.assertEqual(existing.import_batch_id, "existing-batch")

    def test_replace_rolls_back_when_every_row_is_invalid_or_skipped(self) -> None:
        BuildingDetail.objects.create(
            property=self.property_record,
            account_number="PROP001",
            building_number=1,
        )
        skipped = RowResult(values=(), field_names=BUILDING_FIELD_ORDER, skip=True)
        invalid = RowResult(values=(), field_names=BUILDING_FIELD_ORDER, invalid=True)

        with self.assertRaises(UnsafeReplacementError) as raised:
            self._persist(
                OrmPersistenceAdapter(),
                [skipped, invalid],
                write_mode=PersistenceWriteMode.REPLACE,
            )

        self.assertEqual(raised.exception.dataset, PersistenceDataset.BUILDING)
        self.assertEqual((raised.exception.invalid, raised.exception.skipped), (1, 1))
        self.assertTrue(BuildingDetail.objects.filter(account_number="PROP001").exists())

    def test_schema_mismatch_and_incomplete_identity_are_rejected(self) -> None:
        mismatched = RowResult(
            values=self._building_row().values,
            field_names=tuple(reversed(BUILDING_FIELD_ORDER)),
        )
        with self.assertRaises(TranslatedRowSchemaMismatch):
            self._persist(
                OrmPersistenceAdapter(),
                [mismatched],
                write_mode=PersistenceWriteMode.REPLACE,
            )

        with self.assertRaises(IncompletePersistenceIdentity):
            self._persist(
                OrmPersistenceAdapter(),
                [self._building_row(building_number=None)],
                write_mode=PersistenceWriteMode.ADD_MISSING,
            )

    def test_factory_persists_building_with_the_active_adapter(self) -> None:
        result = persistence_for_connection().persist(
            PersistenceRequest(
                dataset=PersistenceDataset.BUILDING,
                rows=iter([self._building_row()]),
                write_mode=PersistenceWriteMode.REPLACE,
            )
        )

        self.assertEqual(result.loaded, 1)
        self.assertTrue(BuildingDetail.objects.filter(account_number="PROP001").exists())
