"""County-owned staging for the Brazos detailed snapshot and its PACS facts."""

from dataclasses import replace
from uuid import uuid4

from django.db import connection

from counties.brazos.models import (
    BrazosPropertySnapshot,
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)
from counties.brazos.property_import import (
    AnnualRefreshStage,
    BrazosPropertyImport,
    PropertyImportRequest,
)
from counties.common.candidate_staging import (
    compute_dataset_hash,
    staged_candidate_schema,
    switch_search_path,
)
from counties.common.import_coverage import compare_coverage
from counties.common.models import ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption

PROPERTY_MODELS = (
    PropertyAccount,
    PropertyLand,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
)
MODELS = (*PROPERTY_MODELS, PropertyJurisdictionExemption, BrazosPropertySnapshot)


def dataset_identity(schema: str = "public") -> dict:
    return compute_dataset_hash(
        MODELS,
        schema=schema,
        scope_filters={PropertyJurisdictionExemption: "AND county = 'brazos'"},
    )


def published_identity() -> dict:
    snapshot = BrazosPropertySnapshot.objects.filter(is_active=True).first()
    return {
        **dataset_identity(),
        "snapshot_id": snapshot.pk if snapshot else None,
        "tax_year": snapshot.tax_year if snapshot else None,
        "outcome": snapshot.outcome if snapshot else None,
    }


def candidate_tables(candidate):
    return switch_search_path(candidate.storage_schema)


class _CandidateSource:
    """Keep actual county source inspection beside candidate-only persistence."""

    def __init__(self, stage: AnnualRefreshStage, operation: ImportOperation):
        self.stage, self.operation, self.name = stage, operation, stage.name

    def prepare(self, options):
        preparation = self.stage.prepare(options)
        measured = self.stage.inspect(preparation, self.operation)
        self.operation.evidence.setdefault("inspection", {})[self.name] = dict(measured.metrics)
        return preparation

    def persist(self, preparation):
        return self.stage.persist(preparation)

    def cleanup(self, preparation):
        return None


def prepare_candidate(cad, gis, request: PropertyImportRequest, operation: ImportOperation):
    from counties.brazos.property_coverage import outcome_populations
    from counties.brazos.property_import import PropertyImportMode
    from counties.common.import_retention import baseline_sources, retain_baseline_sources

    if connection.vendor != "postgresql":
        raise ValueError("Durable Brazos candidate preparation requires PostgreSQL")
    inherited = (
        baseline_sources("brazos") if request.mode is PropertyImportMode.GIS_RECOVERY else []
    )
    candidate = ImportCandidate.objects.create(
        county="brazos",
        operation=operation,
        storage_schema="brazos_candidate_" + uuid4().hex,
        baseline=published_identity(),
        request={"mode": request.mode.value, "tax_year": request.options.tax_year},
        evidence={"publication": "Published data unchanged"},
    )
    operation.evidence["candidate_id"] = str(candidate.pk)
    previous = outcome_populations()
    operation.evidence["source_validation"] = {
        "valid": False,
        "database_publication": "Database publication untested",
    }
    try:
        importer = BrazosPropertyImport(
            _CandidateSource(cad, operation), _CandidateSource(gis, operation) if gis else None
        )
        with staged_candidate_schema(
            "brazos",
            candidate,
            MODELS,
            shared_models_scope={PropertyJurisdictionExemption: " WHERE county = 'brazos'"},
        ):
            result = importer._run_unstaged(
                replace(
                    request,
                    prepare_only=False,
                    replay=None,
                    options=replace(request.options, keep_extracted=True),
                ),
                operation,
            )
            candidate.evidence["population"] = {
                model._meta.model_name: model.objects.filter(tax_year=result.tax_year).count()
                for model in PROPERTY_MODELS
            }
            candidate.evidence["coverage"] = compare_coverage(
                previous,
                outcome_populations(
                    claimed_gis=request.mode is not PropertyImportMode.CAD_RECOVERY,
                    deliberately_absent_gis=request.mode is PropertyImportMode.CAD_RECOVERY,
                ),
            )
            candidate.evidence["coordinate_provenance"] = list(
                PropertyAccount.objects.filter(tax_year=result.tax_year)
                .exclude(coordinate_source="")
                .values("prop_id", "coordinate_source", "coordinate_source_year")
            )
        operation.evidence["source_validation"]["valid"] = True
        candidate.request["tax_year"] = result.tax_year
        operation.requested_year = result.tax_year
        candidate.evidence.update(
            {
                "outcome": result.outcome.value,
                "tax_year": result.tax_year,
                "cad": dict(result.cad.metrics) if result.cad else None,
                "gis": dict(result.gis.metrics) if result.gis else None,
                "capabilities": (
                    "GIS capabilities unavailable"
                    if result.gis is None
                    else "Year-matched GIS inspected"
                ),
                "inspection": operation.evidence.get("inspection", {}),
            }
        )
        candidate.state = "prepared"
        candidate.evidence["content_identity"] = dataset_identity(candidate.storage_schema)
        coverage = candidate.evidence["coverage"]
        if coverage["hard_failures"]:
            candidate.state = "blocked"
        elif coverage["requires_review"]:
            candidate.state = "awaiting_review"
        return replace(
            result,
            snapshot_id=None,
            prepared=True,
            candidate_id=candidate.pk,
            workflow_state=candidate.state,
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
