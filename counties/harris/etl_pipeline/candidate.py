"""Harris answers to the shared Candidate lifecycle, including exact source replay."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from django.conf import settings

from counties.common.candidate_lifecycle import CandidateLoad
from counties.common.candidate_ports import CandidateTables
from counties.common.import_coverage import OutcomePopulation
from counties.common.import_recovery import ReplayRejected, copy_exact_source
from counties.common.import_states import CandidateState
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord
from counties.harris.readiness import outcome_populations, published_year
from counties.harris.source_catalog import HarrisImportStage, HcadSourceId

from .config import DataSource, ETLConfig
from .extract import ExtractManager
from .import_plan import HarrisImportPlan
from .orchestrator import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisPrepare,
    HarrisReplay,
    harris_candidate_load,
)

MODELS = (PropertyRecord, BuildingDetail, ExtraFeature)


def source_roots() -> tuple[Path, ...]:
    """The roots under which Harris imports keep managed sources, read on every call."""
    return (Path(settings.HCAD_DOWNLOAD_DIR), Path(settings.HCAD_EXTRACT_DIR))


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
        current = ImportCandidate.objects.filter(
            county="harris", state=CandidateState.PUBLISHED
        ).first()
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
            replay=HarrisReplay(
                property_source_year=source.evidence.get("property_source_year"),
                seed=partial(seed_sources, source),
            ),
        )


def seed_sources(
    replayed: ImportCandidate,
    config: ETLConfig,
    sources: list[DataSource],
    operation: ImportOperation,
) -> None:
    """Copy the replayed candidate's retained full inputs exactly into the working roots."""
    retained = replayed.sources
    owners = {
        item.get("source_operation_id") or str(replayed.operation_id)
        for item in retained
        if item.get("source_id") == HcadSourceId.REAL_ACCOUNT_OWNER.value
    }
    for owner in ImportCandidate.objects.filter(county="harris", operation_id__in=owners):
        options = owner.request.get("property_file", {})
        if options.get("append") or options.get("limit") is not None:
            raise ReplayRejected(
                "Automatic recovery is unavailable for append or limited publications; "
                "a reviewed county import of complete source inputs is required"
            )
    manager = ExtractManager(config)
    for source in sources:
        selected = [item for item in retained if item.get("source_id") == source.source_id.value]
        if not selected:
            raise ReplayRejected(f"Retained full Harris inputs are unavailable for {source.name}")
        copied = set()
        root = manager.get_extract_path(source)
        for item in selected:
            original = Path(item["path"])
            if original.suffix.lower() == ".zip":
                destination = manager.get_archive_path(source)
            else:
                parent = next(
                    (parent for parent in original.parents if parent.name == root.name), None
                )
                relative = original.relative_to(parent) if parent else Path(original.name)
                destination = root / relative
            if destination not in copied:
                copy_exact_source(item, destination)
                copied.add(destination)
                if original.suffix.lower() == ".zip":
                    operation.evidence.setdefault("acquired_sources", {})[source.filename] = {
                        "source_url": item.get("source_url"),
                        "source_year": item.get("source_year"),
                    }


__all__ = [
    "MODELS",
    "outcome_populations",
]
