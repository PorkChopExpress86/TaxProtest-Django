"""County-owned staging for the Brazos detailed snapshot and its PACS facts."""

import hashlib
import re
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

from django.db import connection

from counties.brazos.annual_refresh import AnnualRefreshStage
from counties.brazos.models import (
    BrazosPropertySnapshot,
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)
from counties.brazos.property_import import BrazosPropertyImport, PropertyImportRequest
from counties.brazos.source_validation import inspect_cad, inspect_gis
from counties.common.import_coverage import compare_coverage
from counties.common.import_writers import fenced_write
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
    if schema != "public" and not re.fullmatch(r"brazos_candidate_[0-9a-f]{32}", schema):
        raise ValueError("Invalid Brazos dataset storage identity")
    quoted_schema = connection.ops.quote_name(schema)
    digest = hashlib.sha256()
    with connection.cursor() as cursor:
        for model in MODELS:
            table = connection.ops.quote_name(model._meta.db_table)
            scope = "AND county = 'brazos'" if model is PropertyJurisdictionExemption else ""
            digest.update(table.encode())
            last_id = 0
            while True:
                cursor.execute(
                    f"SELECT id, row_to_json(t)::text FROM {quoted_schema}.{table} t WHERE id > %s {scope} ORDER BY id LIMIT 1000",
                    [last_id],
                )
                rows = cursor.fetchall()
                if not rows:
                    break
                for row_id, record in rows:
                    digest.update(record.encode())
                    digest.update(b"\n")
                    last_id = row_id
    return {"sha256": digest.hexdigest()}


def published_identity() -> dict:
    snapshot = BrazosPropertySnapshot.objects.filter(is_active=True).first()
    return {
        **dataset_identity(),
        "snapshot_id": snapshot.pk if snapshot else None,
        "tax_year": snapshot.tax_year if snapshot else None,
        "outcome": snapshot.outcome if snapshot else None,
    }


@contextmanager
def candidate_tables(candidate):
    if not re.fullmatch(r"brazos_candidate_[0-9a-f]{32}", candidate.storage_schema):
        raise ValueError("Invalid Brazos candidate storage identity")
    with connection.cursor() as cursor:
        cursor.execute("SHOW search_path")
        previous = cursor.fetchone()[0]
        cursor.execute(f'SET search_path TO "{candidate.storage_schema}", public')
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('search_path', %s, false)", [previous])


class _CandidateSource:
    """Keep actual county source inspection beside candidate-only persistence."""

    def __init__(self, stage: AnnualRefreshStage, operation: ImportOperation):
        self.stage, self.operation, self.name = stage, operation, stage.name

    def prepare(self, options):
        preparation = self.stage.prepare(options)
        if self.name == "cad":
            measured = inspect_cad(self.operation, preparation)
        else:
            measured = inspect_gis(self.operation, preparation)
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
        with fenced_write(), connection.cursor() as cursor:
            schema = connection.ops.quote_name(candidate.storage_schema)
            cursor.execute(f"CREATE SCHEMA {schema}")
            for model in MODELS:
                table = connection.ops.quote_name(model._meta.db_table)
                cursor.execute(f"CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING ALL)")
                cursor.execute(
                    "SELECT pg_get_serial_sequence(%s, 'id')",
                    [f"{candidate.storage_schema}.{model._meta.db_table}"],
                )
                if cursor.fetchone()[0] is None:
                    sequence = connection.ops.quote_name(model._meta.db_table + "_id_seq")
                    cursor.execute(
                        f"CREATE SEQUENCE {schema}.{sequence} OWNED BY {schema}.{table}.id"
                    )
                    cursor.execute(
                        f"ALTER TABLE {schema}.{table} ALTER COLUMN id SET DEFAULT nextval(%s::regclass)",
                        [f"{candidate.storage_schema}.{model._meta.db_table}_id_seq"],
                    )
                scope = " WHERE county = 'brazos'" if model is PropertyJurisdictionExemption else ""
                cursor.execute(
                    f"INSERT INTO {schema}.{table} OVERRIDING SYSTEM VALUE SELECT * FROM public.{table}{scope}"
                )
                cursor.execute(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE((SELECT MAX(id) FROM "
                    f"{schema}.{table}), 1), EXISTS(SELECT 1 FROM {schema}.{table}))",
                    [f"{candidate.storage_schema}.{model._meta.db_table}"],
                )
        importer = BrazosPropertyImport(
            _CandidateSource(cad, operation), _CandidateSource(gis, operation) if gis else None
        )
        with candidate_tables(candidate):
            result = importer._run_unstaged(
                replace(
                    request,
                    prepare_only=False,
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
        candidate.save()
