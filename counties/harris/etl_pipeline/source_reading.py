"""HCAD source reading: which extracted HCAD tabular files feed which dataset of an import,
and the translated rows each dataset yields.

One reading is built per import and shared by source validation and the staged load, so the
files validation approves are the files the load reads, translated the same way with the same
room counts. A file is matched only through the HCAD source catalog's declared member
patterns, anywhere under the extract directory of the source that declares it and in that
source only. What a member means to an import (its dataset) is import knowledge and lives
here, not in the catalog. GIS sources are not read here.

The residential account map that Building and Extra Feature rows resolve accounts through is
an input: each consumer supplies its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from functools import cached_property
from pathlib import Path

from counties.harris.source_catalog import (
    DEFAULT_HCAD_SOURCE_CATALOG,
    DataSource,
    DataSourceType,
    HcadSourceId,
)

from .fixtures_aggregator import FixturesAggregator
from .row_reader import (
    FixtureLookup,
    RowResult,
    iter_building_rows,
    iter_extra_feature_rows,
    iter_property_rows,
)


class HcadDataset(StrEnum):
    """What an extracted member feeds; each value is the schema name evidence stores."""

    PROPERTY = "real_acct"
    BUILDING = "building_res"
    EXTRA_FEATURE = "extra_features"
    ROOM_COUNTS = "fixtures"


# The catalog member, by source and label, that feeds each dataset.
_MEMBER_DATASETS: dict[tuple[HcadSourceId, str], HcadDataset] = {
    (HcadSourceId.REAL_ACCOUNT_OWNER, "real_acct.txt"): HcadDataset.PROPERTY,
    (HcadSourceId.REAL_BUILDING_LAND, "building_res.txt"): HcadDataset.BUILDING,
    (HcadSourceId.REAL_BUILDING_LAND, "extra_features*.txt"): HcadDataset.EXTRA_FEATURE,
    (HcadSourceId.REAL_BUILDING_LAND, "fixtures.txt"): HcadDataset.ROOM_COUNTS,
}
# Of the Extra Feature member's patterns, the one whose files supersede the others'.
_DETAIL_PATTERN = "extra_features_detail*.txt"


@dataclass(frozen=True)
class ReadMember:
    """One extracted file and the dataset it feeds."""

    path: Path
    dataset: HcadDataset


# Extra Feature ordinals: loadable rows seen per (account, code, number) in one pass.
_Ordinals = dict[tuple[str, str, int], int]


@dataclass(frozen=True)
class TranslatedFile:
    """One selected file of a dataset, and the translated rows it yields.

    The files of one ``translated_files`` call share that pass's Extra Feature ordinals, so
    a feature's ``source_ordinal`` continues across them: consume each file's rows before
    the next file's.
    """

    path: Path
    dataset: HcadDataset
    _room_counts: Callable[[], FixtureLookup] = field(repr=False, compare=False)
    _ordinals: _Ordinals = field(repr=False, compare=False)

    def rows(self, account_map: Mapping[str, int] | None = None) -> Iterator[RowResult]:
        """Translate the file; Building and Extra Feature rows resolve through ``account_map``.

        Property rows need no account map; one given is not used.
        """
        if self.dataset is HcadDataset.PROPERTY:
            return iter_property_rows(self.path)
        if account_map is None:
            raise ValueError(f"{self.dataset.value} rows need the residential account map")
        if self.dataset is HcadDataset.BUILDING:
            return iter_building_rows(self.path, account_map, self._room_counts())
        return iter_extra_feature_rows(self.path, account_map, ordinals=self._ordinals)


class _DatasetFiles:
    """The files of each dataset, over the source readings a reading holds, in their order."""

    def _source_readings(self) -> tuple[SourceReading, ...]:
        raise NotImplementedError

    def _source_files(self, dataset: HcadDataset) -> list[tuple[SourceReading, Path]]:
        return [
            (source, member.path)
            for source in self._source_readings()
            for member in source.members
            if member.dataset is dataset
        ]

    def files(self, dataset: HcadDataset) -> tuple[Path, ...]:
        """The files of ``dataset``, source by source, in path order within a source."""
        return tuple(path for _, path in self._source_files(dataset))

    def translated_files(self, dataset: HcadDataset) -> tuple[TranslatedFile, ...]:
        """The files of ``dataset``, as ``files`` orders them, as one translation pass."""
        if dataset is HcadDataset.ROOM_COUNTS:
            raise ValueError("Room counts are not translated rows; use room_counts()")
        ordinals: _Ordinals = {}
        return tuple(
            TranslatedFile(path, dataset, _room_counts=source.room_counts, _ordinals=ordinals)
            for source, path in self._source_files(dataset)
        )


@dataclass(frozen=True)
class SourceReading(_DatasetFiles):
    """The reading of one source's extract directory.

    ``members`` are in path order, datasets interleaved, as the files sort under the
    directory. ``missing_required_members`` names the catalog labels of a required source's
    required members that no file matches; an optional source reports none.
    """

    source: DataSource
    extract_dir: Path
    members: tuple[ReadMember, ...]
    missing_required_members: tuple[str, ...]

    def _source_readings(self) -> tuple[SourceReading, ...]:
        return (self,)

    def room_counts(self) -> FixturesAggregator:
        """This source's room counts, read from its room-count member on the first call only.

        A source with no room-count member has none. Of several room-count files, the
        last in path order gives the counts, as the room-count reader replaces its counts
        on every file.
        """
        return self._room_counts

    @cached_property
    def _room_counts(self) -> FixturesAggregator:
        counts = FixturesAggregator()
        for path in self.files(HcadDataset.ROOM_COUNTS):
            counts.load_fixtures_file(path)
        return counts

    def translated_members(self, *datasets: HcadDataset) -> tuple[TranslatedFile, ...]:
        """This source's files of ``datasets`` in path order, datasets interleaved as in
        ``members``; each dataset's files are one translation pass of their own."""
        passes = {dataset: iter(self.translated_files(dataset)) for dataset in datasets}
        return tuple(
            next(passes[member.dataset]) for member in self.members if member.dataset in passes
        )


@dataclass(frozen=True)
class HcadSourceReading(_DatasetFiles):
    """The reading of an import's tabular sources, in the order they were given."""

    sources: tuple[SourceReading, ...]

    def _source_readings(self) -> tuple[SourceReading, ...]:
        return self.sources

    def source_reading(self, source: DataSource) -> SourceReading:
        """The reading of ``source``, one of the tabular sources this reading was built from."""
        for reading in self.sources:
            if reading.source == source:
                return reading
        raise LookupError(f"HCAD source reading holds no source {source.name}")

    def read_room_counts(self) -> None:
        """Read every source's room counts now, unless already read; later passes reuse them."""
        for source in self.sources:
            source.room_counts()


def read_hcad_source(source: DataSource, extract_dir: Path) -> SourceReading:
    """Read one tabular source's extract directory; an absent directory holds no files."""
    files = sorted(extract_dir.rglob("*.txt"))
    datasets = [
        (member, dataset)
        for member in DEFAULT_HCAD_SOURCE_CATALOG.members(source.source_id)
        if (dataset := _MEMBER_DATASETS.get((source.source_id, member.label))) is not None
    ]
    selected = [
        ReadMember(path, dataset)
        for path in files
        for member, dataset in datasets
        if _matches(path, member.patterns)
    ]
    detail = [
        member
        for member in selected
        if member.dataset is HcadDataset.EXTRA_FEATURE and _matches(member.path, (_DETAIL_PATTERN,))
    ]
    if detail:
        selected = [
            member
            for member in selected
            if member.dataset is not HcadDataset.EXTRA_FEATURE or member in detail
        ]
    return SourceReading(
        source=source,
        extract_dir=extract_dir,
        members=tuple(selected),
        missing_required_members=tuple(
            DEFAULT_HCAD_SOURCE_CATALOG.missing_required_members(source.source_id, files)
        ),
    )


def read_hcad_sources(sources: Iterable[tuple[DataSource, Path]]) -> HcadSourceReading:
    """Read each tabular source of an import from its extract directory; GIS is skipped."""
    return HcadSourceReading(
        sources=tuple(
            read_hcad_source(source, extract_dir)
            for source, extract_dir in sources
            if source.source_type is not DataSourceType.GIS_DATA
        )
    )


def _matches(path: Path, patterns: tuple[str, ...]) -> bool:
    name = path.name.lower()
    return any(fnmatch(name, pattern) for pattern in patterns)


__all__ = [
    "HcadDataset",
    "HcadSourceReading",
    "ReadMember",
    "SourceReading",
    "TranslatedFile",
    "read_hcad_source",
    "read_hcad_sources",
]
