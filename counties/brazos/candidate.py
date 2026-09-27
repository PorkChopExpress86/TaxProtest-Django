"""Brazos answers to the shared Candidate lifecycle, including exact source replay."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

from counties.brazos.cad_refresh import ALL_FILENAMES, CadRefreshStage, _CadStagePayload
from counties.brazos.gis_refresh import GisRefreshStage, GisSourcePayload
from counties.brazos.models import BrazosPropertySnapshot
from counties.brazos.property_import import (
    MODELS,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
    StagePreparation,
    brazos_candidate_load,
    outcome_populations,
)
from counties.common.candidate_lifecycle import CandidateLoad
from counties.common.candidate_ports import CandidateTables
from counties.common.import_coverage import OutcomePopulation
from counties.common.import_recovery import ReplayRejected, copy_exact_source
from counties.common.import_writers import working_source_root
from counties.common.models import ImportCandidate
from counties.common.tax_models import PropertyJurisdictionExemption


class BrazosCandidatePort:
    """What the shared Candidate lifecycle asks Brazos."""

    tables = CandidateTables(MODELS, county_scoped=frozenset({PropertyJurisdictionExemption}))

    def published(self) -> dict:
        snapshot = BrazosPropertySnapshot.objects.filter(is_active=True).first()
        return {
            "snapshot_id": snapshot.pk if snapshot else None,
            "tax_year": snapshot.tax_year if snapshot else None,
            "outcome": snapshot.outcome if snapshot else None,
        }

    def outcomes(self, candidate: ImportCandidate | None) -> dict[str, OutcomePopulation]:
        if candidate is None:
            return outcome_populations()
        partial = candidate.request["mode"] == PropertyImportMode.CAD_RECOVERY.value
        return outcome_populations(claimed_gis=not partial, deliberately_absent_gis=partial)

    def replay(self, source: ImportCandidate) -> CandidateLoad:
        partial = source.evidence["outcome"] == "partial"
        return brazos_candidate_load(
            RetainedCadStage(source.sources),
            None if partial else RetainedGisStage(source.sources),
            PropertyImportRequest(
                mode=PropertyImportMode.CAD_RECOVERY if partial else PropertyImportMode.ANNUAL,
                options=RefreshOptions(
                    tax_year=source.request["tax_year"], skip_download=True, skip_extract=True
                ),
                prepare_only=True,
            ),
        )


def _copy_archive(sources):
    item = next((item for item in sources if Path(item["path"]).suffix.lower() == ".zip"), None)
    if item is None:
        return None
    destination = (
        working_source_root(Path(settings.BCAD_DOWNLOAD_DIR), reuse=False) / Path(item["path"]).name
    )
    copy_exact_source(item, destination)
    return destination


class RetainedCadStage(CadRefreshStage):
    """Read a candidate's retained CAD inputs exactly instead of acquiring them."""

    def __init__(self, sources):
        super().__init__()
        self.sources = [item for item in sources if item.get("stage") == "cad"]

    def prepare(self, options):
        if not self.sources:
            raise ReplayRejected("Retained CAD sources are unavailable")
        year = self.sources[0]["source_year"]
        root = working_source_root(Path(settings.BCAD_EXTRACT_DIR), reuse=False) / str(year)
        files = {}
        for filename in ALL_FILENAMES:
            item = next(
                (
                    item
                    for item in self.sources
                    if Path(item["path"]).name.upper().endswith(filename)
                ),
                None,
            )
            if item is None:
                raise ReplayRejected(f"Retained CAD input unavailable: {filename}")
            destination = root / Path(item["path"]).name
            copy_exact_source(item, destination)
            files[filename] = destination
        archive = _copy_archive(self.sources)
        return StagePreparation(
            "cad",
            year,
            options.tax_year or year,
            _CadStagePayload(files, root, archive, self.sources[0].get("source_url") or ""),
            (),
        )


class RetainedGisStage(GisRefreshStage):
    """Read a candidate's retained GIS layer exactly instead of acquiring it."""

    def __init__(self, sources):
        super().__init__()
        self.sources = [item for item in sources if item.get("stage") == "gis"]

    def prepare(self, options):
        shape = next(
            (item for item in self.sources if Path(item["path"]).suffix.lower() == ".shp"), None
        )
        if shape is None:
            raise ReplayRejected("Retained GIS layer unavailable")
        original = Path(shape["path"])
        year = shape["source_year"]
        root = working_source_root(Path(settings.BCAD_EXTRACT_DIR), reuse=False) / "gis" / str(year)
        for item in self.sources:
            path = Path(item["path"])
            if path.parent == original.parent and path.stem == original.stem:
                copy_exact_source(item, root / path.name)
        archive = _copy_archive(self.sources)
        return StagePreparation(
            "gis",
            year,
            options.tax_year or year,
            GisSourcePayload(root / original.name, root, archive, shape.get("source_url") or ""),
            (),
        )
