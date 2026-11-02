"""The Brazos property-import stage contract, shared by the orchestrator and the stages.

The request, preparation, result and stage types cross between the orchestrator
(``property_import``), the CAD and GIS stage adapters, source inspection and the
candidate replay. The replay payloads are in-memory carriers only; nothing in this
module is persisted, so making them public touches no evidence (ADR-0024).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from counties.brazos.models import SnapshotOutcome
from counties.common.import_states import CandidateState
from counties.common.models import ImportOperation


@dataclass(frozen=True)
class RefreshOptions:
    """The narrow operator interface for a complete annual refresh or property import."""

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
class CadStagePayload:
    """The certified CAD inputs a preparation carries from acquisition to persistence."""

    text_files: dict[str, Path]
    extract_dir: Path
    archive: Path | None = None
    source_url: str = ""


@dataclass(frozen=True)
class GisSourcePayload:
    """The parcel shapefile a GIS preparation carries from acquisition to persistence."""

    shapefile_path: Path | None
    extract_dir: Path
    archive: Path | None = None
    source_url: str = ""


class PropertyImportStage(Protocol):
    """A source-specific adapter used by the Brazos property import."""

    name: str

    def prepare(self, options: RefreshOptions) -> StagePreparation: ...

    def inspect(self, preparation: StagePreparation, operation: ImportOperation) -> StageResult: ...

    def persist(self, preparation: StagePreparation) -> StageResult: ...


AnnualRefreshStage = PropertyImportStage


class PropertyImportMode(StrEnum):
    """The command-adapter request kind accepted by the property import."""

    ANNUAL = "annual"
    CAD_RECOVERY = "cad_recovery"
    GIS_RECOVERY = "gis_recovery"


PropertyImportOutcome = SnapshotOutcome


@dataclass(frozen=True)
class PropertyImportRequest:
    """Immutable operator request crossing the property-import seam."""

    mode: PropertyImportMode
    options: RefreshOptions
    actor: str = ""
    origin: str = "operator"
    prepare_only: bool = False
    candidate_id: UUID | None = None
    application_reason: str = ""

    def __post_init__(self):
        if self.candidate_id is not None and (self.options.dry_run or self.prepare_only):
            raise ValueError("Candidate application requires explicit publication intent")


@dataclass(frozen=True)
class PropertyImportResult:
    """Immutable result of a successfully staged property import."""

    tax_year: int
    outcome: PropertyImportOutcome
    cad: StageResult | None
    gis: StageResult | None
    snapshot_id: int | None
    dry_run: bool
    cleanup_warnings: tuple[str, ...] = ()
    operation_id: UUID | None = None
    candidate_id: UUID | None = None
    prepared: bool = False
    workflow_state: str = CandidateState.PUBLISHED
    already_applied: bool = False
