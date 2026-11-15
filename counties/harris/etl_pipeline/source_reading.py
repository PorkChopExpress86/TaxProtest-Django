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


class _RoomCountsMemo:
    """The one read of a source's room-count member, kept for every later pass."""

    def __init__(self) -> None:
        self.counts: FixturesAggregator | None = None


@dataclass(frozen=True)
class SourceReading:
    """The reading of one source's extract directory.

    ``members`` are in path order, datasets interleaved, as the files sort under the
    directory. ``missing_required_members`` names the catalog labels of a required source's
    required members that no file matches; an optional source reports none.
    """

    source: DataSource
    extract_dir: Path
    members: tuple[ReadMember, ...]
    missing_required_members: tuple[str, ...]
    _room_counts: _RoomCountsMemo = field(
        default_factory=_RoomCountsMemo, init=False, repr=False, compare=False
    )

    def files(self, dataset: HcadDataset) -> tuple[Path, ...]:
        return tuple(member.path for member in self.members if member.dataset is dataset)

    def room_counts(self) -> FixturesAggregator:
        """This source's room counts, read from its room-count member on the first call only.

        A source with no room-count member has none. Of several room-count files, the
        last in path order gives the counts, as the room-count reader replaces its counts
        on every file.
        """
        if self._room_counts.counts is None:
            counts = FixturesAggregator()
            for path in self.files(HcadDataset.ROOM_COUNTS):
                counts.load_fixtures_file(path)
            self._room_counts.counts = counts
        return self._room_counts.counts

    def translated_files(self, dataset: HcadDataset) -> tuple[TranslatedFile, ...]:
        """This source's files of ``dataset``, in path order, as one translation pass."""
        return _translated_files(dataset, [(self, path) for path in self.files(dataset)])


@dataclass(frozen=True)
class HcadSourceReading:
    """The reading of an import's tabular sources, in the order they were given."""

    sources: tuple[SourceReading, ...]

    def files(self, dataset: HcadDataset) -> tuple[Path, ...]:
        return tuple(path for source in self.sources for path in source.files(dataset))

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

    def translated_files(self, dataset: HcadDataset) -> tuple[TranslatedFile, ...]:
        """Every source's files of ``dataset``, source by source, as one translation pass."""
        return _translated_files(
            dataset, [(source, path) for source in self.sources for path in source.files(dataset)]
        )


def _translated_files(
    dataset: HcadDataset, files: list[tuple[SourceReading, Path]]
) -> tuple[TranslatedFile, ...]:
    if dataset is HcadDataset.ROOM_COUNTS:
        raise ValueError("Room counts are not translated rows; use room_counts()")
    ordinals: _Ordinals = {}
    return tuple(
        TranslatedFile(path, dataset, _room_counts=source.room_counts, _ordinals=ordinals)
        for source, path in files
    )


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
    if any(
        member.dataset is HcadDataset.EXTRA_FEATURE and _matches(member.path, (_DETAIL_PATTERN,))
        for member in selected
    ):
        selected = [
            member
            for member in selected
            if member.dataset is not HcadDataset.EXTRA_FEATURE
            or _matches(member.path, (_DETAIL_PATTERN,))
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


def _check_mapping() -> None:
    """Fail at import if the mapping names a member or pattern the catalog does not declare."""
    for (source_id, label), dataset in _MEMBER_DATASETS.items():
        member = next(
            (m for m in DEFAULT_HCAD_SOURCE_CATALOG.members(source_id) if m.label == label), None
        )
        if member is None:
            raise LookupError(f"HCAD source {source_id} declares no member {label}")
        if dataset is HcadDataset.EXTRA_FEATURE and _DETAIL_PATTERN not in member.patterns:
            raise LookupError(f"HCAD member {label} declares no pattern {_DETAIL_PATTERN}")


_check_mapping()


__all__ = [
    "HcadDataset",
    "HcadSourceReading",
    "ReadMember",
    "SourceReading",
    "TranslatedFile",
    "read_hcad_source",
    "read_hcad_sources",
]
