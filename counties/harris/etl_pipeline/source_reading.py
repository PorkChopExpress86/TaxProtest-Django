"""HCAD source reading: which extracted HCAD tabular files feed which dataset of an import.

Source validation and the staged load both ask this module, so the files validation approves
are the files the load reads. A file is matched only through the HCAD source catalog's
declared member patterns, anywhere under the extract directory of the source that declares
it and in that source only. What a member means to an import (its dataset) is import
knowledge and lives here, not in the catalog. GIS sources are not read here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path

from counties.harris.source_catalog import (
    DEFAULT_HCAD_SOURCE_CATALOG,
    DataSource,
    DataSourceType,
    HcadSourceId,
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

    def files(self, dataset: HcadDataset) -> tuple[Path, ...]:
        return tuple(member.path for member in self.members if member.dataset is dataset)


@dataclass(frozen=True)
class HcadSourceReading:
    """The reading of an import's tabular sources, in the order they were given."""

    sources: tuple[SourceReading, ...]

    def files(self, dataset: HcadDataset) -> tuple[Path, ...]:
        return tuple(path for source in self.sources for path in source.files(dataset))


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
    "read_hcad_source",
    "read_hcad_sources",
]
