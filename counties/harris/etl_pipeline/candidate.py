"""Harris-owned durable staging of the existing translated property contract."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import connection

from counties.common.analysis import MIN_COMPS_FOR_RECOMMENDATION
from counties.common.candidate_staging import (
    compute_dataset_hash,
    cutover_staged_tables,
    staged_candidate_schema,
    switch_search_path,
)
from counties.common.import_coverage import OutcomePopulation, compare_coverage
from counties.common.import_recovery import ReplayRejected, copy_exact_source
from counties.common.import_retention import (
    baseline_sources,
    record_publication,
    retain_baseline_sources,
)
from counties.common.import_review import authorize_publication
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate
from counties.harris.adapter import adapter
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord
from counties.harris.source_catalog import HarrisImportStage

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


def outcome_populations(year: int | None, *, claimed_gis=False) -> dict[str, OutcomePopulation]:
    buildings: dict[str, tuple[object, object, object]] = {}
    for (
        acct,
        heat_area,
        bedrooms,
        bathrooms,
    ) in (
        BuildingDetail.objects.filter(is_active=True)
        .order_by("id")
        .values_list("account_number", "heat_area", "bedrooms", "bathrooms")
        .iterator(chunk_size=10000)
    ):
        if acct not in buildings:
            buildings[acct] = (heat_area, bedrooms, bathrooms)
    ready, located, equity_inputs, exclusions = set(), set(), set(), {}
    any_coordinates = False
    for (
        key,
        is_residential,
        is_data_ready,
        latitude,
        longitude,
        building_area,
        assessed_value,
        val,
    ) in PropertyRecord.objects.values_list(
        "account_number",
        "is_residential",
        "is_data_ready",
        "latitude",
        "longitude",
        "building_area",
        "assessed_value",
        "value",
    ).iterator(
        chunk_size=10000
    ):
        has_coords = latitude is not None and longitude is not None
        any_coordinates |= has_coords
        reasons = []
        if not is_residential:
            reasons.append("Not residential under Harris source classification")
        if not is_data_ready:
            b_info = buildings.get(key)
            if not has_coords:
                reasons.append("Coordinates unavailable")
            if b_info is None or b_info[1] is None or b_info[2] is None:
                reasons.append("Active bedroom/bathroom facts unavailable")
            if not reasons:
                reasons.append("Harris data readiness incomplete")
        if reasons:
            exclusions[key] = reasons
        else:
            ready.add(key)
            if has_coords:
                located.add(key)
        b_info = buildings.get(key)
        area = b_info[0] if (b_info and b_info[0]) else building_area
        value = assessed_value or val
        if area and area > 0 and value and value > 0:
            equity_inputs.add(key)
    report_supported = len(equity_inputs) >= MIN_COMPS_FOR_RECOMMENDATION + 1
    report_pool = ready & equity_inputs & located
    report_pool_supported = len(report_pool) >= MIN_COMPS_FOR_RECOMMENDATION + 1
    report_ready = set()
    report_exclusions = dict(exclusions)
    for key in ready:
        if key not in equity_inputs or key not in located:
            report_exclusions[key] = [
                "Positive assessed value, living area and coordinates are required"
            ]
        elif not report_pool_supported:
            report_exclusions[key] = ["At least three qualifying comparables are required"]
        else:
            report_ready.add(key)
    tax_prerequisites = bool(
        year
        and report_supported
        and TaxUnitRate.objects.filter(county="harris", tax_year=year).exists()
        and PropertyJurisdictionExemption.objects.filter(county="harris", tax_year=year).exists()
    )
    tax_ready, tax_exclusions = set(), dict(report_exclusions)
    if tax_prerequisites and report_ready:
        exempt_accounts = set(
            PropertyJurisdictionExemption.objects.filter(
                county="harris",
                tax_year=year,
                account_number__in=report_ready,
            )
            .values_list("account_number", flat=True)
            .distinct()
        )
        for key in report_ready:
            if key in exempt_accounts:
                tax_ready.add(key)
            else:
                tax_exclusions[key] = [
                    "Matching-year jurisdiction and exemption rows are unavailable"
                ]
    tax_supported = bool(tax_ready)
    return {
        "search": OutcomePopulation(ready, exclusions=exclusions),
        "comparable": OutcomePopulation(
            located,
            supported=claimed_gis or any_coordinates,
            reason="" if claimed_gis or any_coordinates else "GIS coordinates unavailable",
            exclusions=exclusions,
        ),
        "report": OutcomePopulation(
            report_ready,
            supported=report_supported,
            reason=(
                ""
                if report_supported
                else "A qualifying equity comparison population is unavailable"
            ),
            exclusions=report_exclusions,
        ),
        "tax": OutcomePopulation(
            tax_ready,
            supported=tax_supported,
            reason=(
                ""
                if tax_supported
                else "Matching-year report, jurisdiction and rate prerequisites unavailable"
            ),
            exclusions=tax_exclusions,
        ),
    }


def publish_candidate(candidate_id, operation, *, user=None):
    with fenced_write():
        candidate = ImportCandidate.objects.select_for_update().get(
            pk=candidate_id, county="harris"
        )
        if candidate.state in ("published", "superseded"):
            operation.publication_before = operation.publication_after = published_identity()
            operation.evidence["already_applied"] = str(candidate.pk)
            return candidate
        review = authorize_publication(candidate, user=user)
        operation.publication_before = published_identity()
        cutover_staged_tables(candidate, MODELS, drop_staged=False)
        record_publication(candidate, operation)
        operation.publication_after = {
            **published_identity(),
            "candidate_id": str(candidate.pk),
            "data_year": candidate.request["data_year"],
            "property_source_year": candidate.evidence.get("property_source_year"),
        }
        operation.status = "published"
        operation.evidence["qualified_publication"] = "Observed atomic publication"
        operation.evidence["candidate_id"] = str(candidate.pk)
        operation.save()
        ImportAuditEntry.objects.create(
            operation=candidate.operation,
            kind="publication",
            actor=operation.actor,
            reason=operation.evidence.get("application_reason") or "Qualified candidate applied",
            evidence={
                "before": operation.publication_before,
                "after": operation.publication_after,
                "review_id": str(review.pk) if review else None,
                "operation_id": str(operation.pk),
            },
            result="published",
        )
    return candidate


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


__all__ = [
    "MODELS",
    "candidate_tables",
    "dataset_identity",
    "outcome_populations",
    "prepare_candidate",
    "prepare_recovery",
    "publish_candidate",
    "published_identity",
    "seed_sources",
]
