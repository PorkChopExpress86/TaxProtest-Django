"""Stateless tabular transformation and persistence pipeline for Harris County."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Any

from django.db import DatabaseError

from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

from .config import DataSource
from .logging import ETLLogger
from .persistence import (
    PersistenceDataset,
    PersistenceRequest,
    PersistenceWriteMode,
    UnsafeReplacementError,
    persistence_for_connection,
)
from .row_reader import RowResult
from .source_reading import HcadDataset, HcadSourceReading, TranslatedFile


@dataclass(frozen=True)
class SourceFileCounts:
    loaded: int = 0
    invalid: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class TabularSourceResult:
    loaded: int = 0
    invalid: int = 0
    skipped: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    did_work: bool = False


class HarrisSourcePipeline:
    """Persists the translated rows of the import's HCAD source reading, source by source.

    ``reading`` is the HCAD source reading the import built once and validated; room counts
    come from it. The load supplies the residential account map, read from the staged table
    after the property persist.
    """

    def __init__(
        self,
        *,
        reading: HcadSourceReading,
        property_file: Any = None,
        logger: ETLLogger | Any = None,
    ) -> None:
        self.reading = reading
        self.property_file = property_file
        self.logger = logger
        self._account_to_property: dict[str, int] | None = None

    def invalidate_account_map(self) -> None:
        """Clear cached residential account mappings."""
        self._account_to_property = None

    def get_account_to_property_map(self) -> dict[str, int]:
        """Return the per-import residential account map used by translation."""
        if self._account_to_property is None:
            from counties.harris.models import PropertyRecord

            self._account_to_property = dict(
                PropertyRecord.objects.filter(is_residential=True).values_list(
                    "account_number", "id"
                )
            )
            if self.logger:
                self.logger.info(
                    f"Loaded {len(self._account_to_property)} account->property mappings"
                )
        return self._account_to_property

    def preload_fixtures(self) -> None:
        """Make sure the reading's room counts are read before any building is translated.

        Source validation has normally read them already; the reading reads them only once.
        """
        if self.logger:
            self.logger.info("Pre-loading fixtures for bedroom/bathroom data...")

        building_sources = [
            source
            for source in self.reading.sources
            if DEFAULT_HCAD_SOURCE_CATALOG.is_source(source.source, HcadSourceId.REAL_BUILDING_LAND)
        ]
        if not building_sources:
            if self.logger:
                self.logger.warning("Real Building Land source not found in sources list")
            return

        if not any(source.files(HcadDataset.ROOM_COUNTS) for source in building_sources):
            if self.logger:
                extract_dirs = ", ".join(str(source.extract_dir) for source in building_sources)
                self.logger.warning(f"Fixtures file not found under {extract_dirs}")
            return

        for source in building_sources:
            stats = source.room_counts().get_stats()
            if self.logger:
                self.logger.info(
                    f"Fixtures loaded: {stats['total_buildings']:,} buildings, "
                    f"{stats['with_bedrooms']:,} with bedrooms, "
                    f"{stats['with_bathrooms']:,} with bathrooms"
                )

    def process_data_file(
        self,
        translated: TranslatedFile,
        *,
        truncate: bool = True,
    ) -> SourceFileCounts:
        """Process a single Property or Building data file."""
        file_path, dataset = translated.path, translated.dataset

        if self.logger:
            self.logger.info(f"Processing {file_path.name} with schema {dataset.value}")

        # The staged table holds no property row to map until the property persist.
        rows = translated.rows(
            None if dataset is HcadDataset.PROPERTY else self.get_account_to_property_map()
        )
        property_file = self.property_file if dataset is HcadDataset.PROPERTY else None
        if property_file:
            if property_file.limit is not None:
                if property_file.limit < 1:
                    raise ValueError("limit must be at least one")
                rows = islice(rows, property_file.limit)
            truncate = not property_file.append

        if dataset in {HcadDataset.PROPERTY, HcadDataset.BUILDING}:
            persisted = persistence_for_connection(
                orm_batch_size=property_file.batch_size if property_file else 5000
            ).persist(
                PersistenceRequest(
                    dataset=(
                        PersistenceDataset.PROPERTY
                        if dataset is HcadDataset.PROPERTY
                        else PersistenceDataset.BUILDING
                    ),
                    rows=rows,
                    write_mode=(
                        PersistenceWriteMode.REPLACE
                        if truncate
                        else PersistenceWriteMode.ADD_MISSING
                    ),
                )
            )
            if dataset is HcadDataset.PROPERTY:
                self.invalidate_account_map()
            return SourceFileCounts(
                loaded=persisted.loaded,
                invalid=persisted.invalid,
                skipped=persisted.skipped,
                failed=0,
            )

        return SourceFileCounts()

    def process_extra_feature_files(self, files: Sequence[TranslatedFile]) -> SourceFileCounts:
        """Persist every selected Extra Feature source as one logical dataset."""

        def translated_rows() -> Iterator[RowResult]:
            for translated in files:
                yield from translated.rows(self.get_account_to_property_map())

        persisted = persistence_for_connection().persist(
            PersistenceRequest(
                dataset=PersistenceDataset.EXTRA_FEATURE,
                rows=translated_rows(),
                write_mode=PersistenceWriteMode.REPLACE,
            )
        )
        return SourceFileCounts(
            loaded=persisted.loaded,
            invalid=persisted.invalid,
            skipped=persisted.skipped,
            failed=0,
        )

    def process_tabular_source(
        self,
        source: DataSource,
        *,
        strict: bool = True,
    ) -> TabularSourceResult:
        """Persist one tabular source's translated rows.

        Source validation has already failed the import for a required source whose
        extract directory or required members are missing; an optional source with no
        extract directory is only a warning.
        """
        reading = self.reading.source_reading(source)
        if not source.required:
            extract_path = reading.extract_dir
            if not extract_path.exists():
                msg = f"Extract path not found for {source.name}: {extract_path}"
                if self.logger:
                    self.logger.warning(msg)
                return TabularSourceResult(warnings=(msg,))

        schema_loaded: dict[HcadDataset, bool] = {}
        extra_feature_files = reading.translated_files(HcadDataset.EXTRA_FEATURE)
        total_loaded = total_invalid = total_skipped = total_failed = 0
        source_errors: list[str] = []
        source_did_work = False

        # Property and Building files are persisted one by one, in path order.
        data_files = reading.translated_members(HcadDataset.PROPERTY, HcadDataset.BUILDING)
        for translated in data_files:
            dataset = translated.dataset
            truncate = not schema_loaded.get(dataset, False)
            try:
                counts = self.process_data_file(translated, truncate=truncate)
            except (DatabaseError, OSError, UnsafeReplacementError, ValueError) as exc:
                message = f"Failed processing {translated.path.name}: {exc}"
                source_errors.append(message)
                total_failed += 1
                if self.logger:
                    self.logger.error(message)
                if strict:
                    break
                continue
            schema_loaded[dataset] = True
            source_did_work = True
            total_loaded += counts.loaded
            total_invalid += counts.invalid
            total_skipped += counts.skipped
            total_failed += counts.failed

        if not (strict and source_errors) and extra_feature_files:
            try:
                counts = self.process_extra_feature_files(extra_feature_files)
            except (DatabaseError, OSError, UnsafeReplacementError, ValueError) as exc:
                message = f"Failed processing Extra Feature dataset: {exc}"
                source_errors.append(message)
                total_failed += 1
                if self.logger:
                    self.logger.error(message)
            else:
                source_did_work = True
                total_loaded += counts.loaded
                total_invalid += counts.invalid
                total_skipped += counts.skipped
                total_failed += counts.failed

        return TabularSourceResult(
            loaded=total_loaded,
            invalid=total_invalid,
            skipped=total_skipped,
            failed=total_failed,
            errors=tuple(source_errors),
            warnings=(),
            did_work=source_did_work,
        )
