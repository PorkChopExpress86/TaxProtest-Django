"""Harris-owned durable staging of the existing translated property contract."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import connection

from counties.common import candidate_ports
from counties.common.candidate_ports import CandidateTables
from counties.common.candidate_staging import (
    staged_candidate_schema,
)
from counties.common.import_coverage import OutcomePopulation, compare_coverage
from counties.common.import_recovery import ReplayRejected, copy_exact_source
from counties.common.import_retention import (
    baseline_sources,
    retain_baseline_sources,
)
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord
from counties.harris.readiness import outcome_populations, published_year
from counties.harris.source_catalog import HarrisImportStage

from .import_plan import HarrisImportPlan

if TYPE_CHECKING:
    from .orchestrator import _HarrisImportExecution

MODELS = (PropertyRecord, BuildingDetail, ExtraFeature)


class HarrisCandidatePort:
    """What the shared Candidate lifecycle asks Harris."""

    tables = CandidateTables(MODELS)

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


def prepare_candidate(execution: _HarrisImportExecution, operation: ImportOperation):
    from .config import DataSourceType
    from .orchestrator import ExtractedSourceRetention, HarrisApply, HarrisImportStatus

    if connection.vendor != "postgresql":
        raise ValueError("Durable Harris candidate preparation requires PostgreSQL")
    inherited = (
        baseline_sources("harris")
        if (not execution.request.plan.is_full or execution.request.property_file is not None)
        else []
    )
    candidate = ImportCandidate.objects.create(
        county="harris",
        operation=operation,
        storage_schema="harris_candidate_" + uuid4().hex,
        baseline=published_identity(),
        request={
            "plan": execution.request.plan.legacy_scope,
            "data_year": execution.data_year,
        },
        evidence={
            "publication": "Published data unchanged",
            "audit": {"data_year": execution.data_year},
        },
    )
    candidate.evidence["property_source_year"] = (
        execution.data_year
        if HarrisImportStage.PROPERTY in execution.request.plan.stages
        and execution.request.property_file is None
        else (
            published_year()
            if HarrisImportStage.PROPERTY not in execution.request.plan.stages
            else None
        )
    )
    if execution.request.replay is not None:
        candidate.evidence["property_source_year"] = operation.evidence["recovery"].get(
            "property_source_year"
        )
    operation.evidence["candidate_id"] = str(candidate.pk)
    assert isinstance(execution.request.load, HarrisApply)
    candidate.request["validate_completeness"] = execution.request.load.validate_completeness
    if execution.request.property_file is not None:
        source = execution.request.property_file
        candidate.request["property_file"] = {
            "path": str(source.path),
            "append": source.append,
            "limit": source.limit,
            "batch_size": source.batch_size,
        }
    previous = outcome_populations(published_year())
    try:
        foreign_keys = [
            (BuildingDetail, "property_id", PropertyRecord, "id"),
            (ExtraFeature, "property_id", PropertyRecord, "id"),
        ]
        execution.request = replace(
            execution.request,
            load=replace(
                execution.request.load,
                extracted_source_retention=ExtractedSourceRetention.RETAIN,
            ),
        )
        with staged_candidate_schema("harris", candidate, MODELS, foreign_keys=foreign_keys):
            result = execution.run()
            if result.status is HarrisImportStatus.COMPLETED:
                coverage = compare_coverage(
                    previous,
                    outcome_populations(
                        candidate.evidence["property_source_year"],
                        claimed_gis=any(
                            source.source_type is DataSourceType.GIS_DATA
                            for source in execution.sources
                        ),
                    ),
                )
                candidate.evidence["coverage"] = coverage
            candidate.evidence["population"] = {
                "properties": PropertyRecord.objects.count(),
                "buildings": BuildingDetail.objects.count(),
                "extra_features": ExtraFeature.objects.count(),
                "ready": PropertyRecord.objects.filter(
                    is_residential=True, is_data_ready=True
                ).count(),
            }
        candidate.sources = operation.evidence.get("sources", [])
        candidate.evidence["result"] = result.to_dict()
        candidate.evidence["content_identity"] = dataset_identity(candidate.storage_schema)
        candidate.state = "prepared" if result.status is HarrisImportStatus.COMPLETED else "blocked"
        status = HarrisImportStatus.PREPARED
        if candidate.state == "prepared":
            if coverage["hard_failures"]:
                candidate.state, status = "blocked", HarrisImportStatus.BLOCKED
            elif coverage["requires_review"]:
                candidate.state, status = "awaiting_review", HarrisImportStatus.AWAITING_REVIEW
        return replace(
            result,
            candidate_id=candidate.pk,
            wrote_data=False,
            status=status if result.status is HarrisImportStatus.COMPLETED else result.status,
        )
    except Exception as exc:
        candidate.state = "blocked"
        candidate.evidence["error"] = str(exc)
        raise
    finally:
        retain_baseline_sources(operation, inherited)
        candidate.sources = operation.evidence.get("sources", [])
        operation.save()
        candidate.save()


__all__ = [
    "MODELS",
    "dataset_identity",
    "outcome_populations",
    "prepare_candidate",
    "prepare_recovery",
    "published_identity",
    "seed_sources",
]
