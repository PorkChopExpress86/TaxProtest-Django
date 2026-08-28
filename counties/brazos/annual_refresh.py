"""Coordinate the complete annual Brazos property refresh.

The module is the county-owned seam for a current-year snapshot. Its two
source stages retain their distinct acquisition and parsing implementations;
this coordinator owns the year contract, transactional publication, and
post-commit cleanup policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from django.core.management.base import CommandError


@dataclass(frozen=True)
class RefreshOptions:
    """The narrow operator interface for a complete annual refresh."""

    tax_year: int | None = None
    source_year: int | None = None
    force: bool = False
    skip_download: bool = False
    skip_extract: bool = False
    dry_run: bool = False
    keep_extracted: bool = False


@dataclass(frozen=True)
class StagePreparation:
    """A source stage prepared for persistence but not yet published."""

    name: str
    source_year: int
    target_year: int
    payload: object
    cleanup_paths: tuple[Path, ...]


@dataclass(frozen=True)
class StageResult:
    """The observable outcome of one persisted refresh stage."""

    name: str
    metrics: Mapping[str, int]


@dataclass(frozen=True)
class AnnualRefreshResult:
    """The stage-aware outcome of a completed or dry-run annual refresh."""

    tax_year: int
    cad: StageResult
    gis: StageResult
    dry_run: bool


class AnnualRefreshStage(Protocol):
    """A source-specific adapter used by the annual-refresh module."""

    name: str

    def prepare(self, options: RefreshOptions) -> StagePreparation: ...

    def persist(self, preparation: StagePreparation) -> StageResult: ...

    def cleanup(self, preparation: StagePreparation) -> None: ...


class BrazosAnnualRefresh:
    """Strict command adapter for a completed property-import publication."""

    def __init__(self, cad: AnnualRefreshStage, gis: AnnualRefreshStage):
        self._cad = cad
        self._gis = gis

    def run(self, options: RefreshOptions) -> AnnualRefreshResult:
        """Require a complete year-matched result from the property import."""
        from counties.brazos.property_import import (
            BrazosPropertyImport,
            PropertyImportMode,
            PropertyImportRequest,
        )

        result = BrazosPropertyImport(self._cad, self._gis).run(
            PropertyImportRequest(mode=PropertyImportMode.ANNUAL, options=options)
        )
        return AnnualRefreshResult(
            tax_year=result.tax_year,
            cad=result.cad or StageResult(name=self._cad.name, metrics={}),
            gis=result.gis or StageResult(name=self._gis.name, metrics={}),
            dry_run=result.dry_run,
        )

    @staticmethod
    def _validate_years(
        target_year: int,
        cad: StagePreparation,
        gis: StagePreparation,
    ) -> None:
        """Reject different source snapshots before either stage can write."""
        if cad.source_year != target_year:
            raise CommandError(
                f"CAD source year {cad.source_year} does not match requested year {target_year}."
            )
        if gis.source_year != target_year:
            raise CommandError(
                f"GIS source year {gis.source_year} does not match requested year {target_year}."
            )


def build_default_refresh(reporter: object) -> BrazosAnnualRefresh:
    """Build the production coordinator from county-owned source stages.

    Management commands are CLI adapters over these stage modules.  Keeping the
    coordinator independent of command classes lets the annual and targeted
    refresh paths share exactly the same source implementation.
    """
    from counties.brazos.cad_refresh import CadRefreshStage
    from counties.brazos.gis_refresh import GisRefreshStage

    return BrazosAnnualRefresh(CadRefreshStage(reporter), GisRefreshStage(reporter))
