"""Harris-owned durable staging of the existing translated property contract."""

from __future__ import annotations

from pathlib import Path

from counties.common import candidate_ports
from counties.common.candidate_ports import CandidateTables
from counties.common.import_coverage import OutcomePopulation
from counties.common.import_recovery import ReplayRejected, copy_exact_source
from counties.common.models import ImportCandidate
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord
from counties.harris.readiness import outcome_populations, published_year
from counties.harris.source_catalog import HarrisImportStage

from .import_plan import HarrisImportPlan

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


def dataset_identity(schema: str = "public") -> dict:
    return candidate_ports.dataset_identity("harris", schema)


def published_identity() -> dict:
    return candidate_ports.published_identity("harris")


def seed_sources(config, sources, operation):
    from .extract import ExtractManager

    retained = operation.evidence["recovery"]["sources"]
    owners = {
        item.get("source_operation_id") or operation.evidence["recovery"]["source_operation_id"]
        for item in retained
        if item.get("source_id") == "real-account-owner"
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
        if source.source_id is None:
            raise ReplayRejected("A retained Harris source must have a catalog identity")
        selected = [item for item in retained if item.get("source_id") == source.source_id.value]
        if not selected:
            raise ReplayRejected(f"Retained full Harris inputs are unavailable for {source.name}")
        copied = set()
        root = manager.get_extract_path(source)
        for item in selected:
            original = Path(item["path"])
            if original.suffix.lower() == ".zip":
                destination = config.download_dir / source.filename
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


def prepare_recovery(candidate, replay, *, actor):
    from .import_plan import HarrisImportPlan
    from .orchestrator import (
        HarrisAcquisitionMode,
        HarrisExtractionMode,
        HarrisImportRequest,
        HarrisPrepare,
        run_harris_import,
    )

    # A full replay cannot substitute the current dataset's dependent facts.
    return run_harris_import(
        HarrisImportRequest(
            plan=HarrisImportPlan.from_legacy_scope("full"),
            data_year=candidate.request["data_year"],
            acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
            extraction=HarrisExtractionMode.REUSE_EXTRACTED,
            load=HarrisPrepare(
                validate_completeness=candidate.request.get("validate_completeness", True)
            ),
            actor=actor,
            origin="admin_recovery",
            replay=replay,
        )
    )


__all__ = [
    "MODELS",
    "dataset_identity",
    "outcome_populations",
    "prepare_recovery",
    "published_identity",
    "seed_sources",
]
