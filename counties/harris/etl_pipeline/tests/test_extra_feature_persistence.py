"""Behavioral contract tests for Extra Feature through the Harris persistence seam."""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from itertools import chain
from pathlib import Path
from unittest import skipUnless

from django.db import connection
from django.test import TestCase, TransactionTestCase

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
from counties.harris.etl_pipeline.row_reader import (
    EXTRA_FEATURE_FIELD_ORDER,
    RowResult,
    iter_extra_feature_rows,
)
from counties.harris.models import ExtraFeature, PropertyRecord


class ExtraFeaturePersistenceContractTests(TestCase):
    def setUp(self) -> None:
        self.property_record = PropertyRecord.objects.create(
            account_number="PROP001",
            address="1 TEST ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )

    def _feature_row(
        self,
        account_number: str = "PROP001",
        feature_number: int | None = 1,
        feature_code: str = "POOL",
        *,
        description: str = "Pool",
    ) -> RowResult:
        return RowResult(
            values=(
                self.property_record.pk,
                account_number,
                feature_number,
                feature_code,
                description,
                1.0,
                None,
                20.0,
                10.0,
                "A",
                "G",
                2018,
                5000.0,
                True,
            ),
            field_names=EXTRA_FEATURE_FIELD_ORDER,
        )

    def _persist(
        self,
        adapter: OrmPersistenceAdapter | CopyPersistenceAdapter,
        rows: Iterable[RowResult],
        *,
        write_mode: PersistenceWriteMode,
    ):
        return HarrisPersistence(adapter).persist(
            PersistenceRequest(
                dataset=PersistenceDataset.EXTRA_FEATURE,
                rows=rows,
                write_mode=write_mode,
            )
        )

    def test_orm_replace_persists_extra_features_with_call_metadata(self) -> None:
        result = self._persist(
            OrmPersistenceAdapter(),
            iter([self._feature_row()]),
            write_mode=PersistenceWriteMode.REPLACE,
        )

        feature = ExtraFeature.objects.get(
            account_number="PROP001", feature_code="POOL", feature_number=1
        )
        self.assertEqual(result.dataset, PersistenceDataset.EXTRA_FEATURE)
        self.assertEqual((result.loaded, result.invalid, result.skipped), (1, 0, 0))
        self.assertFalse(result.invalidated_datasets)
        self.assertEqual(feature.import_batch_id, result.batch_id)
        self.assertIsNotNone(feature.import_date)
        self.assertEqual(feature.created_at, feature.updated_at)

    @skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
    def test_copy_replace_matches_orm_state_and_metadata_contract(self) -> None:
        copy_result = self._persist(
            CopyPersistenceAdapter(),
            iter([self._feature_row()]),
            write_mode=PersistenceWriteMode.REPLACE,
        )
        copy_feature = ExtraFeature.objects.get(feature_code="POOL", feature_number=1)
        copy_snapshot = (
            copy_feature.property_id,
            copy_feature.account_number,
            copy_feature.feature_number,
            copy_feature.feature_code,
            copy_feature.feature_description,
            copy_feature.quantity,
            copy_feature.import_batch_id,
        )

        orm_result = self._persist(
            OrmPersistenceAdapter(),
            iter([self._feature_row()]),
            write_mode=PersistenceWriteMode.REPLACE,
        )
        orm_feature = ExtraFeature.objects.get(feature_code="POOL", feature_number=1)
        orm_snapshot = (
            orm_feature.property_id,
            orm_feature.account_number,
            orm_feature.feature_number,
            orm_feature.feature_code,
            orm_feature.feature_description,
            orm_feature.quantity,
            orm_feature.import_batch_id,
        )

        self.assertEqual((copy_result.loaded, copy_result.invalid, copy_result.skipped), (1, 0, 0))
        self.assertEqual((orm_result.loaded, orm_result.invalid, orm_result.skipped), (1, 0, 0))
        self.assertEqual(copy_snapshot[:-1], orm_snapshot[:-1])
        self.assertEqual(copy_snapshot[-1], copy_result.batch_id)
        self.assertEqual(orm_snapshot[-1], orm_result.batch_id)

    def test_add_missing_skips_existing_and_source_duplicates_without_updates(self) -> None:
        ExtraFeature.objects.create(
            property=self.property_record,
            account_number="PROP001",
            feature_number=1,
            feature_code="POOL",
            feature_description="Original pool",
            import_batch_id="existing-batch",
        )

        result = self._persist(
            OrmPersistenceAdapter(batch_size=1),
            iter(
                [
                    self._feature_row(description="Updated pool"),
                    self._feature_row(feature_number=2, feature_code="GAR"),
                    self._feature_row(feature_number=2, feature_code="GAR"),
                ]
            ),
            write_mode=PersistenceWriteMode.ADD_MISSING,
        )

        existing = ExtraFeature.objects.get(feature_code="POOL", feature_number=1)
        self.assertEqual((result.loaded, result.invalid, result.skipped), (1, 0, 2))
        self.assertEqual(existing.feature_description, "Original pool")
        self.assertEqual(existing.import_batch_id, "existing-batch")

    @skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
    def test_copy_add_missing_matches_orm_duplicate_contract(self) -> None:
        ExtraFeature.objects.create(
            property=self.property_record,
            account_number="PROP001",
            feature_number=1,
            feature_code="POOL",
            feature_description="Original pool",
            import_batch_id="existing-batch",
        )

        result = self._persist(
            CopyPersistenceAdapter(),
            iter(
                [
                    self._feature_row(description="Updated pool"),
                    self._feature_row(feature_number=2, feature_code="GAR"),
                    self._feature_row(feature_number=2, feature_code="GAR"),
                ]
            ),
            write_mode=PersistenceWriteMode.ADD_MISSING,
        )

        existing = ExtraFeature.objects.get(feature_code="POOL", feature_number=1)
        self.assertEqual((result.loaded, result.invalid, result.skipped), (1, 0, 2))
        self.assertEqual(existing.feature_description, "Original pool")
        self.assertEqual(existing.import_batch_id, "existing-batch")

    def test_chained_physical_streams_are_one_logical_replacement(self) -> None:
        result = self._persist(
            OrmPersistenceAdapter(),
            chain(
                iter([self._feature_row(feature_number=1, feature_code="POOL")]),
                iter([self._feature_row(feature_number=2, feature_code="GAR")]),
            ),
            write_mode=PersistenceWriteMode.REPLACE,
        )

        self.assertEqual((result.loaded, result.invalid, result.skipped), (2, 0, 0))
        self.assertEqual(
            set(ExtraFeature.objects.values_list("feature_code", flat=True)),
            {"POOL", "GAR"},
        )
        self.assertEqual(
            set(ExtraFeature.objects.values_list("import_batch_id", flat=True)),
            {result.batch_id},
        )

    @skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
    def test_copy_chained_physical_streams_are_one_logical_replacement(self) -> None:
        result = self._persist(
            CopyPersistenceAdapter(),
            chain(
                iter([self._feature_row(feature_number=1, feature_code="POOL")]),
                iter([self._feature_row(feature_number=2, feature_code="GAR")]),
            ),
            write_mode=PersistenceWriteMode.REPLACE,
        )

        self.assertEqual((result.loaded, result.invalid, result.skipped), (2, 0, 0))
        self.assertEqual(
            set(ExtraFeature.objects.values_list("feature_code", flat=True)),
            {"POOL", "GAR"},
        )
        self.assertEqual(
            set(ExtraFeature.objects.values_list("import_batch_id", flat=True)),
            {result.batch_id},
        )

    def test_unsafe_replace_schema_mismatch_and_incomplete_identity_are_rejected(self) -> None:
        ExtraFeature.objects.create(
            property=self.property_record,
            account_number="PROP001",
            feature_number=1,
            feature_code="POOL",
        )
        skipped = RowResult(values=(), field_names=EXTRA_FEATURE_FIELD_ORDER, skip=True)
        invalid = RowResult(values=(), field_names=EXTRA_FEATURE_FIELD_ORDER, invalid=True)
        with self.assertRaises(UnsafeReplacementError) as raised:
            self._persist(
                OrmPersistenceAdapter(),
                iter([skipped, invalid]),
                write_mode=PersistenceWriteMode.REPLACE,
            )
        self.assertEqual(raised.exception.dataset, PersistenceDataset.EXTRA_FEATURE)
        self.assertTrue(ExtraFeature.objects.filter(feature_code="POOL").exists())

        mismatched = RowResult(
            values=self._feature_row().values,
            field_names=tuple(reversed(EXTRA_FEATURE_FIELD_ORDER)),
        )
        with self.assertRaises(TranslatedRowSchemaMismatch):
            self._persist(
                OrmPersistenceAdapter(),
                iter([mismatched]),
                write_mode=PersistenceWriteMode.ADD_MISSING,
            )

        with self.assertRaises(IncompletePersistenceIdentity):
            self._persist(
                OrmPersistenceAdapter(),
                iter([self._feature_row(feature_number=None)]),
                write_mode=PersistenceWriteMode.ADD_MISSING,
            )

    @skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
    def test_copy_rejects_unsafe_replacement_and_incomplete_identity(self) -> None:
        skipped = RowResult(values=(), field_names=EXTRA_FEATURE_FIELD_ORDER, skip=True)
        invalid = RowResult(values=(), field_names=EXTRA_FEATURE_FIELD_ORDER, invalid=True)

        with self.assertRaises(UnsafeReplacementError) as raised:
            self._persist(
                CopyPersistenceAdapter(),
                iter([skipped, invalid]),
                write_mode=PersistenceWriteMode.REPLACE,
            )

        self.assertEqual(raised.exception.dataset, PersistenceDataset.EXTRA_FEATURE)
        self.assertEqual((raised.exception.invalid, raised.exception.skipped), (1, 1))
        self.assertFalse(ExtraFeature.objects.exists())

        with self.assertRaises(IncompletePersistenceIdentity):
            self._persist(
                CopyPersistenceAdapter(),
                iter([self._feature_row(feature_number=None)]),
                write_mode=PersistenceWriteMode.ADD_MISSING,
            )

    def test_translation_marks_every_missing_identity_part_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "extra_features.txt"
            source.write_text(
                "acct\tbld_num\tcd\n"
                "\t1\tPOOL\n"
                "PROP001\t\tPOOL\n"
                "PROP001\t1\t\n"
                "PROP001\t1\tPOOL\n"
                "UNKNOWN\t1\tPOOL\n",
                encoding="latin-1",
            )
            rows = list(iter_extra_feature_rows(source, {"PROP001": self.property_record.pk}))

        self.assertEqual(sum(row.invalid for row in rows), 4)
        self.assertEqual(sum(not row.invalid and not row.skip for row in rows), 1)

    def test_factory_persists_extra_features_with_the_active_adapter(self) -> None:
        result = persistence_for_connection().persist(
            PersistenceRequest(
                dataset=PersistenceDataset.EXTRA_FEATURE,
                rows=iter([self._feature_row()]),
                write_mode=PersistenceWriteMode.REPLACE,
            )
        )

        self.assertEqual(result.loaded, 1)
        self.assertTrue(ExtraFeature.objects.filter(feature_code="POOL").exists())


@skipUnless(connection.vendor == "postgresql", "COPY requires PostgreSQL")
class CopyExtraFeatureReplacementSafetyTests(TransactionTestCase):
    def test_unsafe_replace_rolls_back_the_truncate(self) -> None:
        property_record = PropertyRecord.objects.create(
            account_number="PROP001",
            address="1 TEST ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )
        ExtraFeature.objects.create(
            property=property_record,
            account_number="PROP001",
            feature_number=1,
            feature_code="POOL",
        )
        skipped = RowResult(values=(), field_names=EXTRA_FEATURE_FIELD_ORDER, skip=True)
        invalid = RowResult(values=(), field_names=EXTRA_FEATURE_FIELD_ORDER, invalid=True)

        with self.assertRaises(UnsafeReplacementError) as raised:
            HarrisPersistence(CopyPersistenceAdapter()).persist(
                PersistenceRequest(
                    dataset=PersistenceDataset.EXTRA_FEATURE,
                    rows=iter([skipped, invalid]),
                    write_mode=PersistenceWriteMode.REPLACE,
                )
            )

        self.assertEqual((raised.exception.invalid, raised.exception.skipped), (1, 1))
        self.assertTrue(
            ExtraFeature.objects.filter(
                account_number="PROP001",
                feature_number=1,
                feature_code="POOL",
            ).exists()
        )
