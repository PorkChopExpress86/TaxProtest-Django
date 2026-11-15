"""Stateless tabular transformation and persistence pipeline for Harris County."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from django.db import DatabaseError

from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

from .config import DataSource
from .extract import ExtractManager
from .fixtures_aggregator import FixturesAggregator
from .logging import ETLLogger
from .persistence import (
    PersistenceDataset,
    PersistenceRequest,
    PersistenceWriteMode,
    UnsafeReplacementError,
    persistence_for_connection,
)
from .row_reader import RowResult, iter_building_rows, iter_extra_feature_rows, iter_property_rows
from .source_reading import HcadDataset, read_hcad_source, read_hcad_sources


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
    """Encapsulates tabular data transformation, account mapping, and persistence loading."""

    def __init__(
        self,
        *,
        extract_manager: ExtractManager,
        fixtures_aggregator: FixturesAggregator,
        property_file: Any = None,
        logger: ETLLogger | Any = None,
    ) -> None:
        self.extract_manager = extract_manager
        self.fixtures_aggregator = fixtures_aggregator
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

    def preload_fixtures(self, sources: list[DataSource]) -> None:
        """Pre-load the room-count member to extract bedroom/bathroom counts."""
        if self.logger:
            self.logger.info("Pre-loading fixtures for bedroom/bathroom data...")

        building_sources = [
            source
            for source in sources
            if DEFAULT_HCAD_SOURCE_CATALOG.is_source(source, HcadSourceId.REAL_BUILDING_LAND)
        ]
        if not building_sources:
            if self.logger:
                self.logger.warning("Real Building Land source not found in sources list")
            return

        reading = read_hcad_sources(
            (source, self.extract_manager.get_extract_path(source)) for source in building_sources
        )
        fixtures_paths = reading.files(HcadDataset.ROOM_COUNTS)
        if not fixtures_paths:
            if self.logger:
                extract_dirs = ", ".join(str(source.extract_dir) for source in reading.sources)
                self.logger.warning(f"Fixtures file not found under {extract_dirs}")
            return

        try:
            for fixtures_path in fixtures_paths:
                self.fixtures_aggregator.load_fixtures_file(fixtures_path)
        except (OSError, UnicodeError, ValueError, KeyError) as e:
            if self.logger:
                self.logger.error(f"Error loading fixtures: {e}")
            return
        stats = self.fixtures_aggregator.get_stats()
        if self.logger:
            self.logger.info(
                f"Fixtures loaded: {stats['total_buildings']:,} buildings, "
                f"{stats['with_bedrooms']:,} with bedrooms, "
                f"{stats['with_bathrooms']:,} with bathrooms"
            )

    def iter_translated_rows(
        self,
        schema_name: str,
        file_path: Path,
        *,
        feature_ordinals: dict[tuple[str, str, int], int] | None = None,
    ) -> Iterator[RowResult]:
        """Return the shared translation stream for one supported source file."""
        if schema_name == "real_acct":
            return iter_property_rows(file_path)

        account_map = self.get_account_to_property_map()
        if schema_name == "building_res":
            return iter_building_rows(
                file_path,
                account_map,
                self.fixtures_aggregator,
            )
        if schema_name == "extra_features":
            return iter_extra_feature_rows(file_path, account_map, ordinals=feature_ordinals)
        raise ValueError(f"Unsupported translated schema: {schema_name}")

    def process_data_file(
        self,
        file_path: Path,
        dataset: HcadDataset,
        *,
        truncate: bool = True,
    ) -> SourceFileCounts:
        """Process a single Property or Building data file."""
        schema_name = dataset.value

        if self.logger:
            self.logger.info(f"Processing {file_path.name} with schema {schema_name}")

        rows = self.iter_translated_rows(schema_name, file_path)
        property_file = self.property_file if schema_name == "real_acct" else None
        if property_file:
            if property_file.limit is not None:
                if property_file.limit < 1:
                    raise ValueError("limit must be at least one")
                rows = islice(rows, property_file.limit)
            truncate = not property_file.append

        if schema_name in {"real_acct", "building_res"}:
            persisted = persistence_for_connection(
                orm_batch_size=property_file.batch_size if property_file else 5000
            ).persist(
                PersistenceRequest(
                    dataset=(
                        PersistenceDataset.PROPERTY
                        if schema_name == "real_acct"
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
            if schema_name == "real_acct":
                self.invalidate_account_map()
            return SourceFileCounts(
                loaded=persisted.loaded,
                invalid=persisted.invalid,
                skipped=persisted.skipped,
                failed=0,
            )

        return SourceFileCounts()

    def process_extra_feature_files(self, file_paths: list[Path]) -> SourceFileCounts:
        """Persist every selected Extra Feature source as one logical dataset."""

        def translated_rows() -> Iterator[RowResult]:
            ordinals: dict[tuple[str, str, int], int] = {}
            for file_path in file_paths:
                yield from self.iter_translated_rows(
                    "extra_features", file_path, feature_ordinals=ordinals
                )

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

    def _record_source_failure(
        self,
        source: DataSource,
        msg: str,
        *,
        strict: bool,
    ) -> TabularSourceResult:
        """Handle missing source paths or required files based on source criticality."""
        if source.required:
            if self.logger:
                self.logger.error(msg)
            return TabularSourceResult(errors=(msg,), failed=1 if strict else 0)
        if self.logger:
            self.logger.warning(msg)
        return TabularSourceResult(warnings=(msg,))

    def process_tabular_source(
        self,
        source: DataSource,
        *,
        strict: bool = True,
    ) -> TabularSourceResult:
        """Process one tabular DataSource (extract path lookup, validation, and file loading)."""
        extract_path = self.extract_manager.get_extract_path(source)
        if not extract_path.exists():
            msg = f"Extract path not found for {source.name}: {extract_path}"
            return self._record_source_failure(source, msg, strict=strict)

        reading = read_hcad_source(source, extract_path)
        missing = reading.missing_required_members
        if missing:
            msg = f"Required files missing for {source.name}: {', '.join(missing)}"
            return self._record_source_failure(source, msg, strict=strict)

        schema_loaded: dict[HcadDataset, bool] = {}
        extra_feature_files: list[Path] = []
        total_loaded = total_invalid = total_skipped = total_failed = 0
        source_errors: list[str] = []
        source_did_work = False

        for member in reading.members:
            file_path, dataset = member.path, member.dataset
            if dataset is HcadDataset.ROOM_COUNTS:
                continue
            if dataset is HcadDataset.EXTRA_FEATURE:
                extra_feature_files.append(file_path)
                continue
            truncate = not schema_loaded.get(dataset, False)
            try:
                counts = self.process_data_file(file_path, dataset, truncate=truncate)
            except (DatabaseError, OSError, UnsafeReplacementError, ValueError) as exc:
                message = f"Failed processing {file_path.name}: {exc}"
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
