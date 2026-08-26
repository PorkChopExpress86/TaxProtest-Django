"""Authoritative HCAD source catalog and acquisition policy.

This module is deliberately Django-free so build-time and runtime import paths
can agree on source identity, archive URLs, requiredness, and fallback years.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum, StrEnum
from fnmatch import fnmatch
from typing import Any

logger = logging.getLogger(__name__)


class DataSourceType(Enum):
    """Kinds of source files understood by the Harris ETL."""

    PROPERTY_DATA = "property_data"
    GIS_DATA = "gis_data"
    CODE_DESCRIPTIONS = "code_descriptions"
    HEARING_DATA = "hearing_data"


class FileFormat(Enum):
    """Supported archive and source formats."""

    ZIP = "zip"
    TAR = "tar"
    TAR_GZ = "tar.gz"
    CSV = "csv"
    TXT = "txt"
    SHP = "shp"


class HarrisImportStage(StrEnum):
    """A loadable part of a Harris import."""

    PROPERTY = "property"
    BUILDING = "building"
    GIS = "gis"


class HcadSourceId(StrEnum):
    """Stable identities for the HCAD archives managed by this repository."""

    REAL_ACCOUNT_OWNER = "real-account-owner"
    REAL_ACCOUNT_OWNERSHIP_HISTORY = "real-account-ownership-history"
    REAL_BUILDING_LAND = "real-building-land"
    REAL_JUR_EXEMPT = "real-jur-exempt"
    CODE_DESCRIPTION_REAL = "code-description-real"
    PP_FILES = "pp-files"
    CODE_DESCRIPTION_PP = "code-description-pp"
    HEARING_FILES = "hearing-files"
    GIS_PARCELS = "gis-parcels"


@dataclass
class DataSource:
    """Configuration for one HCAD source archive."""

    name: str
    url_template: str
    filename: str
    source_type: DataSourceType
    file_format: FileFormat = FileFormat.ZIP
    required: bool = True
    checksum: str | None = None
    extract_patterns: list[str] = field(default_factory=list)
    priority: int = 100
    source_id: HcadSourceId | None = None
    import_stages: frozenset[HarrisImportStage] = field(default_factory=frozenset)
    download_timeout: int = 300

    def get_url(self, year: int | None = None) -> str:
        """Return the source URL for a given CAMA year."""
        if year is None:
            year = datetime.now().year
        return self.url_template.format(year=year)

    def clone(self) -> DataSource:
        """Return a mutable copy suitable for a caller-owned configuration."""
        return replace(
            self,
            extract_patterns=list(self.extract_patterns),
            import_stages=frozenset(self.import_stages),
        )

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("DataSource name cannot be empty")
        if not self.url_template:
            raise ValueError("DataSource url_template cannot be empty")
        if "{year}" not in self.url_template and "GIS" not in self.url_template:
            logger.warning("DataSource %s URL does not contain {year} placeholder", self.name)


@dataclass(frozen=True)
class HcadSourceCatalog:
    """The single source of truth for HCAD archive facts and fallback policy."""

    sources: tuple[DataSource, ...]

    def all_sources(self) -> list[DataSource]:
        """Return caller-owned copies in deterministic catalog order."""
        return [source.clone() for source in self.sources]

    def ordered_sources(self) -> list[DataSource]:
        """Return caller-owned copies in operational priority order."""
        return sorted(self.all_sources(), key=lambda source: source.priority)

    def required_sources(self) -> list[DataSource]:
        """Return required sources in operational priority order."""
        return [source for source in self.ordered_sources() if source.required]

    def property_sources(self) -> list[DataSource]:
        return [
            source.clone()
            for source in self.sources
            if source.source_type is not DataSourceType.GIS_DATA
        ]

    def gis_sources(self) -> list[DataSource]:
        return [
            source.clone()
            for source in self.sources
            if source.source_type is DataSourceType.GIS_DATA
        ]

    def source_for_id(self, source_id: HcadSourceId) -> DataSource:
        for source in self.sources:
            if source.source_id is source_id:
                return source.clone()
        raise KeyError(f"Unknown HCAD source id: {source_id}")

    def source_for_archive(self, archive: Mapping[str, Any]) -> DataSource:
        """Resolve a legacy archive mapping through the catalog."""
        raw_id = archive.get("source_id")
        if raw_id:
            return self.source_for_id(HcadSourceId(raw_id))

        filename = str(archive["filename"])
        for source in self.sources:
            if source.filename.lower() == filename.lower():
                return source.clone()
        raise KeyError(f"Unknown HCAD archive filename: {filename}")

    def source_id_for(self, source: DataSource) -> HcadSourceId | None:
        """Resolve a catalog identity for static or compatible caller sources."""
        if source.source_id is not None:
            return source.source_id
        for catalog_source in self.sources:
            if (
                catalog_source.filename.lower() == source.filename.lower()
                or catalog_source.name.lower() == source.name.lower()
            ):
                return catalog_source.source_id
        return None

    def import_stages_for(self, source: DataSource) -> frozenset[HarrisImportStage]:
        """Return stage membership, with a safe fallback for caller-defined sources."""
        if source.import_stages:
            return source.import_stages

        source_id = self.source_id_for(source)
        if source_id is not None:
            return self.source_for_id(source_id).import_stages
        if source.source_type is DataSourceType.GIS_DATA:
            return frozenset({HarrisImportStage.GIS})
        if source.source_type is DataSourceType.PROPERTY_DATA:
            return frozenset({HarrisImportStage.PROPERTY})
        return frozenset()

    def is_source(self, source: DataSource, source_id: HcadSourceId) -> bool:
        return self.source_id_for(source) is source_id

    def candidate_years(self, reference_year: int | None = None) -> list[int]:
        """Prefer the requested CAMA year and then its predecessor."""
        year = reference_year or datetime.now().year
        return [year, year - 1] if year > 2000 else [year]

    def candidate_urls(self, source: DataSource, reference_year: int | None = None) -> list[str]:
        """Return the acquisition URLs in the canonical fallback order."""
        if "{year}" not in source.url_template:
            return [source.get_url(reference_year)]
        return [source.get_url(year) for year in self.candidate_years(reference_year)]

    def legacy_archives(self) -> list[dict[str, Any]]:
        """Adapt catalog facts for the retained legacy download task."""
        return [
            {
                "source_id": source.source_id.value if source.source_id else None,
                "filename": source.filename,
                "required": source.required,
                "timeout": source.download_timeout,
            }
            for source in self.sources
        ]

    def missing_required_files(self, source: DataSource, file_paths: list[str]) -> list[str]:
        """Return core translated-row files absent from an extracted source."""
        source_id = self.source_id_for(source)
        requirements: dict[HcadSourceId, tuple[tuple[str, tuple[str, ...]], ...]] = {
            HcadSourceId.REAL_ACCOUNT_OWNER: (("real_acct.txt", ("real_acct.txt",)),),
            HcadSourceId.REAL_BUILDING_LAND: (
                ("building_res.txt", ("building_res.txt",)),
                ("fixtures.txt", ("fixtures.txt",)),
                ("extra_features*.txt", ("extra_features.txt", "extra_features_detail*.txt")),
            ),
        }
        normalized_names = [
            path.replace("\\", "/").rsplit("/", 1)[-1].lower() for path in file_paths
        ]
        missing: list[str] = []
        for label, patterns in requirements.get(source_id, ()):
            if not any(fnmatch(name, pattern) for name in normalized_names for pattern in patterns):
                missing.append(label)
        return missing


DEFAULT_HCAD_SOURCE_CATALOG = HcadSourceCatalog(
    sources=(
        DataSource(
            name="Real Account Owner",
            url_template="https://download.hcad.org/data/CAMA/{year}/Real_acct_owner.zip",
            filename="Real_acct_owner.zip",
            source_type=DataSourceType.PROPERTY_DATA,
            extract_patterns=["real_acct.txt", "owners.txt", "deeds.txt"],
            priority=10,
            source_id=HcadSourceId.REAL_ACCOUNT_OWNER,
            import_stages=frozenset({HarrisImportStage.PROPERTY}),
        ),
        DataSource(
            name="Real Account Ownership History",
            url_template="https://download.hcad.org/data/CAMA/{year}/Real_acct_ownership_history.zip",
            filename="Real_acct_ownership_history.zip",
            source_type=DataSourceType.PROPERTY_DATA,
            required=False,
            priority=90,
            source_id=HcadSourceId.REAL_ACCOUNT_OWNERSHIP_HISTORY,
        ),
        DataSource(
            name="Real Building Land",
            url_template="https://download.hcad.org/data/CAMA/{year}/Real_building_land.zip",
            filename="Real_building_land.zip",
            source_type=DataSourceType.PROPERTY_DATA,
            extract_patterns=[
                "building_res.txt",
                "fixtures.txt",
                "extra_features.txt",
                "extra_features_detail*.txt",
                "land.txt",
            ],
            priority=20,
            source_id=HcadSourceId.REAL_BUILDING_LAND,
            import_stages=frozenset({HarrisImportStage.BUILDING}),
        ),
        DataSource(
            name="Real Jur Exempt",
            url_template="https://download.hcad.org/data/CAMA/{year}/Real_jur_exempt.zip",
            filename="Real_jur_exempt.zip",
            source_type=DataSourceType.PROPERTY_DATA,
            required=False,
            priority=80,
            source_id=HcadSourceId.REAL_JUR_EXEMPT,
        ),
        DataSource(
            name="Code Description Real",
            url_template="https://download.hcad.org/data/CAMA/{year}/Code_description_real.zip",
            filename="Code_description_real.zip",
            source_type=DataSourceType.CODE_DESCRIPTIONS,
            required=False,
            priority=5,
            source_id=HcadSourceId.CODE_DESCRIPTION_REAL,
            download_timeout=120,
        ),
        DataSource(
            name="PP Files",
            url_template="https://download.hcad.org/data/CAMA/{year}/PP_files.zip",
            filename="PP_files.zip",
            source_type=DataSourceType.PROPERTY_DATA,
            required=False,
            priority=85,
            source_id=HcadSourceId.PP_FILES,
        ),
        DataSource(
            name="Code Description PP",
            url_template="https://download.hcad.org/data/CAMA/{year}/Code_description_pp.zip",
            filename="Code_description_pp.zip",
            source_type=DataSourceType.CODE_DESCRIPTIONS,
            required=False,
            priority=6,
            source_id=HcadSourceId.CODE_DESCRIPTION_PP,
            download_timeout=120,
        ),
        DataSource(
            name="Hearing Files",
            url_template="https://download.hcad.org/data/CAMA/{year}/Hearing_files.zip",
            filename="Hearing_files.zip",
            source_type=DataSourceType.HEARING_DATA,
            required=False,
            priority=95,
            source_id=HcadSourceId.HEARING_FILES,
        ),
        DataSource(
            name="GIS Parcels",
            url_template="https://download.hcad.org/data/GIS/Parcels.zip",
            filename="Parcels.zip",
            source_type=DataSourceType.GIS_DATA,
            priority=50,
            source_id=HcadSourceId.GIS_PARCELS,
            import_stages=frozenset({HarrisImportStage.GIS}),
            download_timeout=600,
        ),
    )
)

# Compatibility exports for callers that need the legacy grouped views. Import
# execution selects fresh source copies directly through the catalog.
DEFAULT_PROPERTY_SOURCES = DEFAULT_HCAD_SOURCE_CATALOG.property_sources()
DEFAULT_GIS_SOURCES = DEFAULT_HCAD_SOURCE_CATALOG.gis_sources()


__all__ = [
    "DEFAULT_GIS_SOURCES",
    "DEFAULT_HCAD_SOURCE_CATALOG",
    "DEFAULT_PROPERTY_SOURCES",
    "DataSource",
    "DataSourceType",
    "FileFormat",
    "HarrisImportStage",
    "HcadSourceId",
    "HcadSourceCatalog",
]
