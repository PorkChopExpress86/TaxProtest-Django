"""Deep persistence module for Harris translated rows.

The public interface is :class:`HarrisPersistence` and its immutable
``PersistenceRequest`` / ``PersistenceResult`` values.  Source parsing and
HCAD business rules remain in :mod:`row_reader`; concrete adapters own only
database writing and database-owned metadata.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.utils import timezone

from .row_reader import PROPERTY_FIELD_ORDER, RowResult, RowValue

logger = logging.getLogger(__name__)


class PersistenceDataset(StrEnum):
    """Logical Harris datasets supported by the persistence seam."""

    PROPERTY = "property"
    BUILDING = "building"
    EXTRA_FEATURE = "extra_feature"


class PersistenceWriteMode(StrEnum):
    """The two supported intents for writing one logical dataset."""

    REPLACE = "replace"
    ADD_MISSING = "add_missing"


class PersistenceError(RuntimeError):
    """Base class for deterministic persistence-contract failures."""


class InvalidPersistenceRequest(PersistenceError):
    """Raised when a request is not supported by this persistence slice."""


class TranslatedRowSchemaMismatch(PersistenceError):
    """Raised when a loadable row does not match the selected dataset schema."""


class IncompletePersistenceIdentity(PersistenceError):
    """Raised when a loadable row cannot be safely deduplicated."""


class UnsafeReplacementError(PersistenceError):
    """Raised when a replacement contains no loadable rows."""

    def __init__(
        self,
        *,
        dataset: PersistenceDataset,
        invalid: int,
        skipped: int,
    ) -> None:
        self.dataset = dataset
        self.invalid = invalid
        self.skipped = skipped
        super().__init__(
            f"Refusing to replace {dataset.value}: no loadable rows "
            f"(invalid={invalid}, skipped={skipped})"
        )


@dataclass(frozen=True)
class PersistenceRequest:
    """One stateless request to persist a logical Harris dataset."""

    dataset: PersistenceDataset
    rows: Iterable[RowResult]
    write_mode: PersistenceWriteMode

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, PersistenceDataset):
            raise InvalidPersistenceRequest("dataset must be a PersistenceDataset")
        if not isinstance(self.write_mode, PersistenceWriteMode):
            raise InvalidPersistenceRequest("write_mode must be a PersistenceWriteMode")


@dataclass(frozen=True)
class PersistenceResult:
    """Observable outcome of one persisted logical Harris dataset."""

    dataset: PersistenceDataset
    write_mode: PersistenceWriteMode
    loaded: int
    invalid: int
    skipped: int
    batch_id: str
    invalidated_datasets: frozenset[PersistenceDataset]


@dataclass(frozen=True)
class _PersistenceMetadata:
    """Database-owned call metadata shared by either concrete adapter."""

    timestamp: datetime
    batch_id: str


class _PropertyRows:
    """One-pass Property rows with shared contract validation and accounting."""

    def __init__(self, rows: Iterable[RowResult]) -> None:
        self._rows = iter(rows)
        self.invalid = 0
        self.skipped = 0
        self.candidates = 0
        self._iterated = False

    def __iter__(self) -> Iterator[RowResult]:
        if self._iterated:
            raise InvalidPersistenceRequest("persistence rows may only be consumed once")
        self._iterated = True
        for row in self._rows:
            if row.skip:
                self.skipped += 1
                continue
            if row.invalid:
                self.invalid += 1
                continue
            if row.field_names != PROPERTY_FIELD_ORDER:
                raise TranslatedRowSchemaMismatch(
                    "Property persistence requires the canonical Property field order"
                )
            account_number = row.as_dict()["account_number"]
            if not isinstance(account_number, str) or not account_number.strip():
                raise IncompletePersistenceIdentity(
                    "Property persistence requires a non-empty account_number"
                )
            self.candidates += 1
            yield row

    def require_safe_replacement(self, mode: PersistenceWriteMode) -> None:
        if mode is PersistenceWriteMode.REPLACE and self.candidates == 0:
            raise UnsafeReplacementError(
                dataset=PersistenceDataset.PROPERTY,
                invalid=self.invalid,
                skipped=self.skipped,
            )


class PropertyPersistenceAdapter(Protocol):
    """The internal adapter seam for writing validated Property rows."""

    def persist_property(
        self,
        rows: _PropertyRows,
        *,
        write_mode: PersistenceWriteMode,
        metadata: _PersistenceMetadata,
    ) -> int:
        """Write the rows and return the number inserted."""


class OrmPersistenceAdapter:
    """Django ORM persistence adapter for translated Property rows."""

    def __init__(
        self,
        *,
        batch_size: int = 5_000,
        database_alias: str = DEFAULT_DB_ALIAS,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        self._batch_size = batch_size
        self._database_alias = database_alias

    def persist_property(
        self,
        rows: _PropertyRows,
        *,
        write_mode: PersistenceWriteMode,
        metadata: _PersistenceMetadata,
    ) -> int:
        from ..models import PropertyRecord

        loaded = 0
        pending: list[RowResult] = []

        def flush() -> None:
            nonlocal loaded
            if not pending:
                return

            unique_pending: list[RowResult] = []
            batch_account_numbers: set[str] = set()
            for row in pending:
                account_number = row.as_dict()["account_number"]
                assert isinstance(account_number, str)
                if account_number in batch_account_numbers:
                    rows.skipped += 1
                    continue
                batch_account_numbers.add(account_number)
                unique_pending.append(row)

            existing = set(
                PropertyRecord.objects.using(self._database_alias)
                .filter(account_number__in=batch_account_numbers)
                .values_list("account_number", flat=True)
            )
            models = []
            inserted_account_numbers: list[str] = []
            for row in unique_pending:
                record = row.as_dict()
                account_number = record["account_number"]
                assert isinstance(account_number, str)
                if account_number in existing:
                    rows.skipped += 1
                    continue
                models.append(PropertyRecord(**record))
                inserted_account_numbers.append(account_number)

            if models:
                PropertyRecord.objects.using(self._database_alias).bulk_create(
                    models,
                    batch_size=self._batch_size,
                )
                PropertyRecord.objects.using(self._database_alias).filter(
                    account_number__in=inserted_account_numbers
                ).update(
                    created_at=metadata.timestamp,
                    updated_at=metadata.timestamp,
                )
                loaded += len(models)
            pending.clear()

        with transaction.atomic(using=self._database_alias):
            if write_mode is PersistenceWriteMode.REPLACE:
                PropertyRecord.objects.using(self._database_alias).all().delete()

            for row in rows:
                pending.append(row)
                if len(pending) >= self._batch_size:
                    flush()
            flush()
            rows.require_safe_replacement(write_mode)

        return loaded


class CopyPersistenceAdapter:
    """PostgreSQL COPY persistence adapter for translated Property rows."""

    _COPY_NULL = r"\N"

    @staticmethod
    def _copy_value(value: RowValue) -> str:
        if value is None:
            return CopyPersistenceAdapter._COPY_NULL
        if isinstance(value, bool):
            return "t" if value else "f"
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\t", "\\t")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )

    def persist_property(
        self,
        rows: _PropertyRows,
        *,
        write_mode: PersistenceWriteMode,
        metadata: _PersistenceMetadata,
    ) -> int:
        from ..models import PropertyRecord

        columns = (*PROPERTY_FIELD_ORDER, "created_at", "updated_at")
        quoted_columns = ", ".join(connection.ops.quote_name(column) for column in columns)
        table = connection.ops.quote_name(PropertyRecord._meta.db_table)
        staging_table = connection.ops.quote_name("harris_property_persistence_stage")
        timestamp = metadata.timestamp.isoformat()

        def copy_lines() -> Iterator[str]:
            for row in rows:
                yield "\t".join(
                    self._copy_value(value) for value in (*row.values, timestamp, timestamp)
                ) + "\n"

        with transaction.atomic(), connection.cursor() as cursor:
            if write_mode is PersistenceWriteMode.REPLACE:
                cursor.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
            cursor.execute(
                f"CREATE TEMPORARY TABLE {staging_table} ON COMMIT DROP AS "
                f"SELECT {quoted_columns} FROM {table} WHERE FALSE"
            )
            cursor.copy_expert(
                f"COPY {staging_table} ({quoted_columns}) FROM STDIN WITH (FORMAT text)",
                _GeneratorIO(copy_lines()),
            )
            rows.require_safe_replacement(write_mode)
            cursor.execute(
                f"INSERT INTO {table} ({quoted_columns}) "
                f"SELECT {quoted_columns} FROM {staging_table} ON CONFLICT DO NOTHING"
            )
            loaded = cursor.rowcount

        rows.skipped += rows.candidates - loaded
        return loaded


class HarrisPersistence:
    """Persist translated Harris rows through one small, testable interface."""

    def __init__(self, adapter: PropertyPersistenceAdapter) -> None:
        self._adapter = adapter

    def persist(self, request: PersistenceRequest) -> PersistenceResult:
        """Persist one logical dataset, propagating operational database failures."""
        if request.dataset is not PersistenceDataset.PROPERTY:
            raise InvalidPersistenceRequest(
                f"{request.dataset.value} persistence is not available in this implementation slice"
            )

        rows = _PropertyRows(request.rows)
        metadata = _PersistenceMetadata(timestamp=timezone.now(), batch_id=uuid4().hex)
        try:
            loaded = self._adapter.persist_property(
                rows,
                write_mode=request.write_mode,
                metadata=metadata,
            )
        except PersistenceError as exc:
            logger.warning(
                "Harris Property persistence rejected for write mode %s: %s",
                request.write_mode.value,
                exc,
            )
            raise
        except Exception:
            logger.exception(
                "Harris Property persistence failed for write mode %s", request.write_mode.value
            )
            raise

        invalidated = (
            frozenset({PersistenceDataset.BUILDING, PersistenceDataset.EXTRA_FEATURE})
            if request.write_mode is PersistenceWriteMode.REPLACE
            else frozenset()
        )
        result = PersistenceResult(
            dataset=request.dataset,
            write_mode=request.write_mode,
            loaded=loaded,
            invalid=rows.invalid,
            skipped=rows.skipped,
            batch_id=metadata.batch_id,
            invalidated_datasets=invalidated,
        )
        logger.info(
            "Persisted Harris Property rows: loaded=%s invalid=%s skipped=%s mode=%s batch=%s",
            result.loaded,
            result.invalid,
            result.skipped,
            result.write_mode.value,
            result.batch_id,
        )
        return result


def persistence_for_connection(*, orm_batch_size: int = 5_000) -> HarrisPersistence:
    """Return the production persistence module for the active database backend."""
    adapter: PropertyPersistenceAdapter
    if connection.vendor == "postgresql":
        adapter = CopyPersistenceAdapter()
    else:
        adapter = OrmPersistenceAdapter(batch_size=orm_batch_size)
    return HarrisPersistence(adapter)


class _GeneratorIO(io.RawIOBase):
    """Expose a text generator as the file object psycopg needs for COPY."""

    def __init__(self, lines: Iterator[str]) -> None:
        self._lines = lines
        self._buffer = ""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunks = [self._buffer, *self._lines]
            self._buffer = ""
            return "".join(chunks).encode("utf-8")
        while len(self._buffer) < size:
            try:
                self._buffer += next(self._lines)
            except StopIteration:
                break
        value, self._buffer = self._buffer[:size], self._buffer[size:]
        return value.encode("utf-8")

    def readinto(self, buffer: bytearray) -> int:  # type: ignore[override]
        value = self.read(len(buffer))
        count = len(value)
        buffer[:count] = value
        return count
