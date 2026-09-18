"""Harris-owned durable staging of the existing translated property contract."""

import hashlib
import re
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from counties.common.candidate_staging import (
    compute_dataset_hash,
    staged_candidate_schema,
    switch_search_path,
)
from counties.common.import_coverage import compare_coverage
from counties.common.import_writers import fenced_write
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord

if TYPE_CHECKING:
    from .orchestrator import _HarrisImportExecution

MODELS = (PropertyRecord, BuildingDetail, ExtraFeature)


def dataset_identity(schema: str = "public") -> dict:
    return compute_dataset_hash(MODELS, schema=schema)


def published_identity() -> dict:
    current = ImportCandidate.objects.filter(county="harris", state="published").first()
    return {
        **dataset_identity(),
        "candidate_id": str(current.pk) if current else None,
        "property_source_year": current.evidence.get("property_source_year") if current else None,
    }


@contextmanager
def candidate_tables(candidate: ImportCandidate):
    """Bind county loaders to their isolated tables and restore the caller path."""
    with switch_search_path(candidate.storage_schema):
        yield


def prepare_candidate(execution: "_HarrisImportExecution", operation: ImportOperation):
    from counties.common.import_retention import baseline_sources, retain_baseline_sources
    from counties.harris.adapter import adapter
    from counties.harris.source_catalog import HarrisImportStage

    from .config import DataSourceType
    from .coverage import outcome_populations
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
        evidence={"publication": "Published data unchanged"},
    )
    candidate.evidence["property_source_year"] = (
        execution.data_year
        if HarrisImportStage.PROPERTY in execution.request.plan.stages
        and execution.request.property_file is None
        else (
            adapter.published_year()
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
    previous = outcome_populations(adapter.published_year())
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
