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


def resolve_schema_name(filename_stem: str) -> str | None:
    """Map a source filename stem to a transform schema name."""
    filename = filename_stem.lower()

    # Skip code description files (lookup tables, not actual data)
    if filename.startswith("desc_"):
        return None

    if filename == "real_acct":
        return "real_acct"
    if filename == "building_res":
        return "building_res"
    if filename == "extra_features" or filename.startswith("extra_features_detail"):
        return "extra_features"
    return None


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
        """Pre-load fixtures.txt to extract bedroom/bathroom counts."""
        if self.logger:
            self.logger.info("Pre-loading fixtures for bedroom/bathroom data...")

        for source in sources:
            if DEFAULT_HCAD_SOURCE_CATALOG.is_source(source, HcadSourceId.REAL_BUILDING_LAND):
                extract_path = self.extract_manager.get_extract_path(source)
                fixtures_path = extract_path / "fixtures.txt"

                if fixtures_path.exists():
                    try:
                        self.fixtures_aggregator.load_fixtures_file(fixtures_path)
                        stats = self.fixtures_aggregator.get_stats()
                        if self.logger:
                            self.logger.info(
                                f"Fixtures loaded: {stats['total_buildings']:,} buildings, "
                                f"{stats['with_bedrooms']:,} with bedrooms, "
                                f"{stats['with_bathrooms']:,} with bathrooms"
                            )
                        return
                    except (OSError, UnicodeError, ValueError, KeyError) as e:
                        if self.logger:
                            self.logger.error(f"Error loading fixtures: {e}")
                        return
                else:
                    if self.logger:
                        self.logger.warning(f"Fixtures file not found: {fixtures_path}")
                    return

        if self.logger:
            self.logger.warning("Real Building Land source not found in sources list")

    def iter_translated_rows(
        self,
        schema_name: str,
        file_path: Path,
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
            return iter_extra_feature_rows(file_path, account_map)
        raise ValueError(f"Unsupported translated schema: {schema_name}")

    def process_data_file(
        self,
        file_path: Path,
        *,
        skip_load: bool = False,
        truncate: bool = True,
    ) -> SourceFileCounts:
        """Process a single tabular data file."""
        filename = file_path.stem.lower()
        schema_name = resolve_schema_name(filename)

        if not schema_name:
            if self.logger:
                self.logger.debug(f"No schema for {file_path.name}, skipping")
            return SourceFileCounts()

        if self.logger:
            self.logger.info(f"Processing {file_path.name} with schema {schema_name}")

        if skip_load:
            loaded = invalid = skipped = failed = 0
            for row in self.iter_translated_rows(schema_name, file_path):
                if row.skip:
                    skipped += 1
                elif row.invalid:
                    invalid += 1
                else:
                    loaded += 1
            return SourceFileCounts(loaded=loaded, invalid=invalid, skipped=skipped, failed=failed)

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

    def process_extra_feature_files(
        self,
        file_paths: list[Path],
        *,
        skip_load: bool = False,
    ) -> SourceFileCounts:
        """Persist every selected Extra Feature source as one logical dataset."""

        def translated_rows() -> Iterator[RowResult]:
            for file_path in file_paths:
                yield from self.iter_translated_rows("extra_features", file_path)

        rows = translated_rows()
        if skip_load:
            loaded = invalid = skipped = failed = 0
            for row in rows:
                if row.skip:
                    skipped += 1
                elif row.invalid:
                    invalid += 1
                else:
                    loaded += 1
            return SourceFileCounts(loaded=loaded, invalid=invalid, skipped=skipped, failed=failed)

        persisted = persistence_for_connection().persist(
            PersistenceRequest(
                dataset=PersistenceDataset.EXTRA_FEATURE,
                rows=rows,
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
        skip_load: bool = False,
        strict: bool = True,
    ) -> TabularSourceResult:
        """Process one tabular DataSource (extract path lookup, validation, and file loading)."""
        extract_path = self.extract_manager.get_extract_path(source)
        if not extract_path.exists():
            msg = f"Extract path not found for {source.name}: {extract_path}"
            return self._record_source_failure(source, msg, strict=strict)

        data_files = sorted(extract_path.rglob("*.txt"))
        missing_required_files = DEFAULT_HCAD_SOURCE_CATALOG.missing_required_files(
            source,
            [str(path) for path in data_files],
        )
        if missing_required_files:
            msg = (
                f"Required files missing for {source.name}: " f"{', '.join(missing_required_files)}"
            )
            return self._record_source_failure(source, msg, strict=strict)

        if DEFAULT_HCAD_SOURCE_CATALOG.is_source(source, HcadSourceId.REAL_BUILDING_LAND):
            has_extra_feature_details = any(
                path.stem.lower().startswith("extra_features_detail") for path in data_files
            )
            if has_extra_feature_details:
                data_files = [path for path in data_files if path.stem.lower() != "extra_features"]

        schema_loaded: dict[str, bool] = {}
        extra_feature_files: list[Path] = []
        total_loaded = total_invalid = total_skipped = total_failed = 0
        source_errors: list[str] = []
        source_did_work = False

        for file_path in data_files:
            schema_name = resolve_schema_name(file_path.stem)
            if schema_name is None:
                continue
            if schema_name == "extra_features":
                extra_feature_files.append(file_path)
                continue
            truncate = not schema_loaded.get(schema_name, False)
            try:
                counts = self.process_data_file(file_path, skip_load=skip_load, truncate=truncate)
            except (DatabaseError, OSError, UnsafeReplacementError, ValueError) as exc:
                message = f"Failed processing {file_path.name}: {exc}"
                source_errors.append(message)
                total_failed += 1
                if self.logger:
                    self.logger.error(message)
                if strict:
                    break
                continue
            schema_loaded[schema_name] = True
            source_did_work = True
            total_loaded += counts.loaded
            total_invalid += counts.invalid
            total_skipped += counts.skipped
            total_failed += counts.failed

        if not (strict and source_errors) and extra_feature_files:
            try:
                counts = self.process_extra_feature_files(
                    extra_feature_files,
                    skip_load=skip_load,
                )
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
