"""Harris answers to the shared Candidate lifecycle, including exact source replay."""

from __future__ import annotations

from counties.common.candidate_lifecycle import CandidateLoad
from counties.common.candidate_ports import CandidateTables
from counties.common.import_coverage import OutcomePopulation
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord
from counties.harris.readiness import outcome_populations, published_year
from counties.harris.source_catalog import HarrisImportStage

from .import_plan import HarrisImportPlan
from .orchestrator import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisPrepare,
    harris_candidate_load,
)

MODELS = (PropertyRecord, BuildingDetail, ExtraFeature)


class HarrisCandidatePort:
    """What the shared Candidate lifecycle asks Harris."""

    tables = CandidateTables(
        MODELS,
        deferred_keys=(
            (BuildingDetail, "property_id", PropertyRecord, "id"),
            (ExtraFeature, "property_id", PropertyRecord, "id"),
        ),
    )

    def published(self) -> dict:
        current = ImportCandidate.objects.filter(county="harris", state="published").first()
        return {
            "candidate_id": str(current.pk) if current else None,
            "property_source_year": (
                current.evidence.get("property_source_year") if current else None
            ),
        }

    def outcomes(self, candidate: ImportCandidate | None) -> dict[str, OutcomePopulation]:
        if candidate is None:
            return outcome_populations(published_year())
        return outcome_populations(
            candidate.evidence.get("property_source_year"),
            claimed_gis=HarrisImportStage.GIS
            in HarrisImportPlan.from_legacy_scope(candidate.request["plan"]).stages,
        )

    def replay(self, source: ImportCandidate, operation: ImportOperation) -> CandidateLoad:
        # A full replay cannot substitute the current dataset's dependent facts.
        return harris_candidate_load(
            HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope("full"),
                data_year=source.request["data_year"],
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisPrepare(
                    validate_completeness=source.request.get("validate_completeness", True)
                ),
            ),
            operation,
            replayed=source,
        )


__all__ = [
    "MODELS",
    "outcome_populations",
]
