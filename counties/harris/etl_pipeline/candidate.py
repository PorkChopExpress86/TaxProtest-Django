"""Harris-owned durable staging of the existing translated property contract."""

import hashlib
import re
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import connection

from counties.common.import_coverage import compare_coverage
from counties.common.import_writers import fenced_write
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord

if TYPE_CHECKING:
    from .orchestrator import _HarrisImportExecution

MODELS = (PropertyRecord, BuildingDetail, ExtraFeature)


def dataset_identity(schema: str = "public") -> dict:
    if schema != "public" and not re.fullmatch(r"harris_candidate_[0-9a-f]{32}", schema):
        raise ValueError("Invalid Harris dataset storage identity")
    quoted_schema = connection.ops.quote_name(schema)
    digest = hashlib.sha256()
    with connection.cursor() as cursor:
        for model in MODELS:
            table = connection.ops.quote_name(model._meta.db_table)
            digest.update(table.encode())
            last_id = 0
            while True:
                cursor.execute(
                    f"SELECT id, row_to_json(t)::text FROM {quoted_schema}.{table} t WHERE id > %s ORDER BY id LIMIT 1000",
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
    return dataset_identity()


@contextmanager
def candidate_tables(candidate: ImportCandidate):
    """Bind county loaders to their isolated tables and restore the caller path."""
    if not re.fullmatch(r"harris_candidate_[0-9a-f]{32}", candidate.storage_schema):
        raise ValueError("Invalid Harris candidate storage identity")
    with connection.cursor() as cursor:
        cursor.execute("SHOW search_path")
        previous = cursor.fetchone()[0]
        cursor.execute(f'SET search_path TO "{candidate.storage_schema}", public')
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('search_path', %s, false)", [previous])


def prepare_candidate(execution: "_HarrisImportExecution", operation: ImportOperation):
    from .config import DataSourceType
    from .coverage import outcome_populations
    from .orchestrator import ExtractedSourceRetention, HarrisImportStatus

    if connection.vendor != "postgresql":
        raise ValueError("Durable Harris candidate preparation requires PostgreSQL")
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
    operation.evidence["candidate_id"] = str(candidate.pk)
    previous_operation = ImportOperation.objects.filter(county="harris", status="published").first()
    previous = outcome_populations(
        previous_operation.requested_year if previous_operation else None
    )
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
                    # Legacy SERIAL defaults copied by LIKE still point at the
                    # public sequence. Give this candidate its own sequence.
                    sequence = connection.ops.quote_name(model._meta.db_table + "_id_seq")
                    cursor.execute(
                        f"CREATE SEQUENCE {schema}.{sequence} OWNED BY {schema}.{table}.id"
                    )
                    cursor.execute(
                        f"ALTER TABLE {schema}.{table} ALTER COLUMN id SET DEFAULT nextval(%s::regclass)",
                        [f"{candidate.storage_schema}.{model._meta.db_table}_id_seq"],
                    )
                cursor.execute(
                    f"INSERT INTO {schema}.{table} OVERRIDING SYSTEM VALUE SELECT * FROM public.{table}"
                )
                cursor.execute(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE((SELECT MAX(id) FROM "
                    f"{schema}.{table}), 1), EXISTS(SELECT 1 FROM {schema}.{table}))",
                    [f"{candidate.storage_schema}.{model._meta.db_table}"],
                )
            # LIKE intentionally does not copy public foreign keys. Candidate
            # cascades and ORM relations must terminate at candidate Property.
            for model in (BuildingDetail, ExtraFeature):
                table = connection.ops.quote_name(model._meta.db_table)
                cursor.execute(
                    f"ALTER TABLE {schema}.{table} ADD FOREIGN KEY (property_id) "
                    f"REFERENCES {schema}.data_propertyrecord(id) DEFERRABLE INITIALLY DEFERRED"
                )
        execution.request = replace(
            execution.request,
            load=replace(
                execution.request.load,
                extracted_source_retention=ExtractedSourceRetention.RETAIN,
            ),
        )
        with candidate_tables(candidate):
            result = execution.run()
            if result.status is HarrisImportStatus.COMPLETED:
                coverage = compare_coverage(
                    previous,
                    outcome_populations(
                        execution.data_year,
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
        candidate.sources = operation.evidence.get("sources", [])
        candidate.save()
