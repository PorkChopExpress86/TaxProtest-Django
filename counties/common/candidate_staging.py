"""County-neutral PostgreSQL candidate schema isolation and publication cutover.

This module provides the database-level mechanism for staging candidate data in
isolated schemas, computing deterministic dataset digests, and atomically cutting
over candidate tables to the public schema. Domain ETL rules, parsing, and outcome
classification remain county-owned.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import connection, models

from counties.common.import_writers import fenced_write

if TYPE_CHECKING:
    from counties.common.models import ImportCandidate

SCHEMA_PATTERN = re.compile(r"^[a-z_]+_candidate_[0-9a-f]{32}$")


def validate_schema_name(schema: str, county: str | None = None) -> None:
    """Ensure the candidate schema name is safe and follows naming conventions."""
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError(f"Invalid candidate storage identity: {schema}")
    if county is not None and not schema.startswith(f"{county}_candidate_"):
        raise ValueError(f"Candidate schema {schema} does not match county {county}")


def compute_dataset_hash(
    models_list: Sequence[type[models.Model]],
    schema: str = "public",
    scope_filters: Mapping[type[models.Model], str] | None = None,
) -> dict[str, str]:
    """Compute a deterministic sha256 checksum across rows of the given models."""
    if schema != "public":
        validate_schema_name(schema)
    quoted_schema = connection.ops.quote_name(schema)
    digest = hashlib.sha256()
    filters = scope_filters or {}
    with connection.cursor() as cursor:
        for model in models_list:
            table = connection.ops.quote_name(model._meta.db_table)
            scope = filters.get(model, "")
            digest.update(table.encode())
            last_id = 0
            while True:
                cursor.execute(
                    f"SELECT id, row_to_json(t)::text FROM {quoted_schema}.{table} t "
                    f"WHERE id > %s {scope} ORDER BY id LIMIT 1000",
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


@contextmanager
def switch_search_path(schema: str) -> Iterator[None]:
    """Temporarily set the PostgreSQL search_path to the candidate schema."""
    if schema != "public":
        validate_schema_name(schema)
    with connection.cursor() as cursor:
        cursor.execute("SHOW search_path")
        previous = cursor.fetchone()[0]
        cursor.execute(f'SET search_path TO "{schema}", public')
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('search_path', %s, false)", [previous])


@contextmanager
def staged_candidate_schema(
    county: str,
    candidate: ImportCandidate,
    models_list: Sequence[type[models.Model]],
    *,
    shared_models_scope: Mapping[type[models.Model], str] | None = None,
    foreign_keys: Sequence[tuple[type[models.Model], str, type[models.Model], str]] | None = None,
) -> Iterator[None]:
    """Provision an isolated candidate schema, clone tables, and scope search_path."""
    if connection.vendor != "postgresql":
        raise ValueError("Durable candidate preparation requires PostgreSQL")

    schema_name = candidate.storage_schema
    validate_schema_name(schema_name, county=county)
    scopes = shared_models_scope or {}

    with fenced_write(), connection.cursor() as cursor:
        quoted_schema = connection.ops.quote_name(schema_name)
        cursor.execute(f"CREATE SCHEMA {quoted_schema}")
        for model in models_list:
            table = connection.ops.quote_name(model._meta.db_table)
            cursor.execute(
                f"CREATE TABLE {quoted_schema}.{table} (LIKE public.{table} INCLUDING ALL)"
            )
            cursor.execute(
                "SELECT pg_get_serial_sequence(%s, 'id')",
                [f"{schema_name}.{model._meta.db_table}"],
            )
            if cursor.fetchone()[0] is None:
                sequence = connection.ops.quote_name(model._meta.db_table + "_id_seq")
                cursor.execute(
                    f"CREATE SEQUENCE {quoted_schema}.{sequence} OWNED BY {quoted_schema}.{table}.id"
                )
                cursor.execute(
                    f"ALTER TABLE {quoted_schema}.{table} ALTER COLUMN id SET DEFAULT nextval(%s::regclass)",
                    [f"{schema_name}.{model._meta.db_table}_id_seq"],
                )
            scope = scopes.get(model, "")
            cursor.execute(
                f"INSERT INTO {quoted_schema}.{table} OVERRIDING SYSTEM VALUE SELECT * FROM public.{table}{scope}"
            )
            cursor.execute(
                "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE((SELECT MAX(id) FROM "
                f"{quoted_schema}.{table}), 1), EXISTS(SELECT 1 FROM {quoted_schema}.{table}))",
                [f"{schema_name}.{model._meta.db_table}"],
            )

        if foreign_keys:
            for source_model, source_col, target_model, target_col in foreign_keys:
                source_table = connection.ops.quote_name(source_model._meta.db_table)
                target_table = connection.ops.quote_name(target_model._meta.db_table)
                quoted_source_col = connection.ops.quote_name(source_col)
                quoted_target_col = connection.ops.quote_name(target_col)
                cursor.execute(
                    f"ALTER TABLE {quoted_schema}.{source_table} ADD FOREIGN KEY ({quoted_source_col}) "
                    f"REFERENCES {quoted_schema}.{target_table}({quoted_target_col}) DEFERRABLE INITIALLY DEFERRED"
                )

    with switch_search_path(schema_name):
        yield


def cutover_staged_tables(
    candidate: ImportCandidate,
    models_list: Sequence[type[models.Model]],
    *,
    shared_models_scope: Mapping[type[models.Model], str] | None = None,
    shared_models_county: Mapping[type[models.Model], str] | None = None,
    drop_staged: bool = False,
) -> None:
    """Atomically cut over staged tables to the public schema and reset sequences."""
    schema_name = candidate.storage_schema
    validate_schema_name(schema_name, county=candidate.county)
    scopes = shared_models_scope or {}
    counties = shared_models_county or {}

    with connection.cursor() as cursor:
        for model in reversed(models_list):
            scope = scopes.get(model, "")
            cursor.execute(
                f"DELETE FROM public.{connection.ops.quote_name(model._meta.db_table)}{scope}"
            )
        for model in models_list:
            table = connection.ops.quote_name(model._meta.db_table)
            if model in counties:
                # County-private candidate sequences must not collide with
                # another county's IDs in this shared table.
                columns = ", ".join(
                    connection.ops.quote_name(field.column)
                    for field in model._meta.fields
                    if field.name != "id"
                )
                cursor.execute(
                    f'INSERT INTO public.{table} ({columns}) SELECT {columns} FROM "{schema_name}".{table} WHERE county = %s',
                    [counties[model]],
                )
                continue
            cursor.execute(
                f'INSERT INTO public.{table} OVERRIDING SYSTEM VALUE SELECT * FROM "{schema_name}".{table}'
            )
            cursor.execute(
                "SELECT pg_get_serial_sequence(%s, 'id')", [f"public.{model._meta.db_table}"]
            )
            sequence = cursor.fetchone()[0]
            cursor.execute(f"SELECT last_value FROM {sequence}")
            last_value = cursor.fetchone()[0]
            cursor.execute(f"SELECT COALESCE(MAX(id), 1) FROM public.{table}")
            maximum = cursor.fetchone()[0]
            cursor.execute(
                "SELECT setval(%s::regclass, %s, true)",
                [sequence, max(last_value, maximum)],
            )

        if drop_staged:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
