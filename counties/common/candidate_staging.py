"""County-neutral PostgreSQL candidate schema isolation and publication cutover.

This module provides the database-level mechanism for staging candidate data in
isolated schemas, computing deterministic dataset digests, and atomically cutting
over candidate tables to the public schema. Domain ETL rules, parsing, and outcome
classification remain county-owned.

Staged tables carry production's exact names so that publication can exchange a
county-owned table whole instead of rewriting its rows (ADR-0025).
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import DatabaseError, OperationalError, connection, models, transaction

from counties.common.import_writers import fenced_write

if TYPE_CHECKING:
    from counties.common.models import ImportCandidate

SCHEMA_PATTERN = re.compile(r"^[a-z_]+_candidate_[0-9a-f]{32}$")
# How long publication waits, per attempt, for shared reads to finish before an exchange.
EXCHANGE_LOCK_TIMEOUT = "5s"
EXCHANGE_LOCK_ATTEMPTS = 3
# A two-key advisory lock, apart from the one-key county writer locks: each shared
# response holds it shared, and an exchange holds it exclusively.
_PUBLISHED_READ_LOCK = (0x54505244, 0)


class CandidateDefinitionMismatch(ValueError):
    """A staged table no longer matches production's definition, so it cannot publish."""


def validate_schema_name(schema: str, county: str | None = None) -> None:
    """Ensure the candidate schema name is safe and follows naming conventions."""
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError(f"Invalid candidate storage identity: {schema}")
    if county is not None and not schema.startswith(f"{county}_candidate_"):
        raise ValueError(f"Candidate schema {schema} does not match county {county}")


class _CopiedRows:
    """Hashes COPY text output as each row's text followed by a newline."""

    def __init__(self, digest: hashlib._Hash) -> None:
        self.digest = digest

    def write(self, data: bytes) -> None:
        # COPY doubles each backslash. A lone one is another COPY escape, such as a raw
        # control character from a json column, so its row's text bytes are unknown.
        if b"\\" in data.replace(b"\\\\", b""):
            raise ValueError("COPY escaped a dataset row; its identity bytes are unknown")
        self.digest.update(data.replace(b"\\\\", b"\\"))


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
            # One COPY streams the table in a single scan that may use parallel workers.
            # "id > 0" keeps the bounds of the keyset scan that made the persisted digests.
            cursor.copy_expert(
                f"COPY (SELECT row_to_json(t)::text FROM {quoted_schema}.{table} t "
                f"WHERE id > 0 {scope} ORDER BY id) TO STDOUT (ENCODING 'UTF8')",
                _CopiedRows(digest),
            )
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
    seed: bool = True,
) -> Iterator[None]:
    """Provision an isolated candidate schema, clone tables, and scope search_path.

    Each table is seeded with its published rows, unless ``seed`` is false because
    the load replaces every table whole.
    """
    if connection.vendor != "postgresql":
        raise ValueError("Durable candidate preparation requires PostgreSQL")

    schema_name = candidate.storage_schema
    validate_schema_name(schema_name, county=county)
    scopes = shared_models_scope or {}

    renames = itertools.count()
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
            _adopt_production_index_names(cursor, schema_name, model, renames)
            if seed:
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
                name = _production_foreign_key_name(cursor, source_model, source_col, target_model)
                constraint = f"CONSTRAINT {connection.ops.quote_name(name)} " if name else ""
                cursor.execute(
                    f"ALTER TABLE {quoted_schema}.{source_table} ADD {constraint}FOREIGN KEY ({quoted_source_col}) "
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
) -> None:
    """Atomically publish staged tables, inside the caller's transaction (ADR-0025).

    A county-owned table is exchanged whole; a shared table scoped by county, or one
    a foreign key or view outside the exchanged set depends on, has its rows replaced.
    A candidate whose staged definitions no longer match production's is refused.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("Publication cuts over only inside the caller's transaction")
    schema_name = candidate.storage_schema
    validate_schema_name(schema_name, county=candidate.county)
    scopes = shared_models_scope or {}
    counties = shared_models_county or {}

    with connection.cursor() as cursor:
        with _qualified_catalog(cursor):
            mismatched = [
                model._meta.db_table
                for model in models_list
                if _definition(cursor, schema_name, model) != _definition(cursor, "public", model)
            ]
            if mismatched:
                raise CandidateDefinitionMismatch(
                    "Staged tables no longer match production, so prepare a new candidate: "
                    + ", ".join(mismatched)
                )
            exchanged = _exchangeable(cursor, models_list, frozenset(counties))
        replaced = [model for model in models_list if model not in exchanged]
        for model in reversed(replaced):
            scope = scopes.get(model, "")
            cursor.execute(
                f"DELETE FROM public.{connection.ops.quote_name(model._meta.db_table)}{scope}"
            )
        for model in replaced:
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
            _advance_sequence(cursor, "public", model, f"public.{table}")
        _exchange(cursor, schema_name, [model for model in models_list if model in exchanged])


def _advance_sequence(cursor, schema: str, model: type[models.Model], ids_from: str) -> None:
    """Move ``schema``'s id sequence for ``model`` past every id in ``ids_from`` and
    past production's last value, so no published id is handed out again."""
    cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [f"public.{model._meta.db_table}"])
    production = cursor.fetchone()[0]
    cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [f"{schema}.{model._meta.db_table}"])
    sequence = cursor.fetchone()[0]
    cursor.execute(f"SELECT last_value FROM {production}")
    last_value = cursor.fetchone()[0]
    cursor.execute(f"SELECT COALESCE(MAX(id), 1) FROM {ids_from}")
    maximum = cursor.fetchone()[0]
    cursor.execute("SELECT setval(%s::regclass, %s, true)", [sequence, max(last_value, maximum)])


def _exchange(cursor, schema_name: str, exchanged: Sequence[type[models.Model]]) -> None:
    """Swap each production table with its staged table; the replaced ones stay in the
    candidate schema, which publication drops."""
    if not exchanged:
        return
    quoted_schema = connection.ops.quote_name(schema_name)
    retiring = connection.ops.quote_name(f"{schema_name}_retiring")
    tables = [connection.ops.quote_name(model._meta.db_table) for model in exchanged]
    for table in tables:
        cursor.execute(f"ANALYZE {quoted_schema}.{table}")
    _lock_for_exchange(cursor, tables)
    for model, table in zip(exchanged, tables, strict=True):
        _advance_sequence(cursor, schema_name, model, f"{quoted_schema}.{table}")
    # Production and staged objects share names, so production steps aside first.
    cursor.execute(f"CREATE SCHEMA {retiring}")
    for table in tables:
        cursor.execute(f"ALTER TABLE public.{table} SET SCHEMA {retiring}")
    for table in tables:
        cursor.execute(f"ALTER TABLE {quoted_schema}.{table} SET SCHEMA public")
    for table in tables:
        cursor.execute(f"ALTER TABLE {retiring}.{table} SET SCHEMA {quoted_schema}")
    cursor.execute(f"DROP SCHEMA {retiring}")


def _lock_for_exchange(cursor, tables: Sequence[str]) -> None:
    """Wait, a bounded time, until no shared response or other reader uses ``tables``."""
    cursor.execute("SHOW lock_timeout")
    previous = cursor.fetchone()[0]
    cursor.execute("SELECT set_config('lock_timeout', %s, true)", [EXCHANGE_LOCK_TIMEOUT])
    for attempt in range(EXCHANGE_LOCK_ATTEMPTS):
        try:
            with transaction.atomic():
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", _PUBLISHED_READ_LOCK)
                cursor.execute(
                    f"LOCK TABLE {', '.join(f'public.{table}' for table in tables)} "
                    "IN ACCESS EXCLUSIVE MODE"
                )
            break
        except OperationalError as exc:
            if getattr(exc.__cause__, "pgcode", None) != "55P03":
                raise
            if attempt + 1 == EXCHANGE_LOCK_ATTEMPTS:
                raise OperationalError(
                    "Publication could not lock the published tables within "
                    f"{EXCHANGE_LOCK_ATTEMPTS} waits of {EXCHANGE_LOCK_TIMEOUT}; "
                    "retry once shared reads finish"
                ) from exc
    cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])


@contextmanager
def published_read() -> Iterator[None]:
    """Keep publication from exchanging tables while one shared response reads them.

    Taken before the response's transaction begins, so its snapshot is never older
    than an exchange that committed while it waited.
    """
    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock_shared(%s, %s)", _PUBLISHED_READ_LOCK)
    try:
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock_shared(%s, %s)", _PUBLISHED_READ_LOCK)
        except DatabaseError:
            pass  # a lost connection released the lock with its session


@contextmanager
def _qualified_catalog(cursor) -> Iterator[None]:
    """Make catalog functions schema-qualify every name, production's included."""
    cursor.execute("SHOW search_path")
    previous = cursor.fetchone()[0]
    cursor.execute("SET search_path TO pg_catalog")
    try:
        yield
    finally:
        cursor.execute("SELECT set_config('search_path', %s, false)", [previous])


def _relation(schema: str, model: type[models.Model]) -> str:
    return f'"{schema}"."{model._meta.db_table}"'


def _schema_neutral(text: str, schema: str) -> str:
    """A catalog definition with its own table's schema removed; other qualifiers stay."""
    for qualifier in (" ON ", "REFERENCES ", "nextval('"):
        text = text.replace(f"{qualifier}{schema}.", qualifier)
    return text


def _definition(cursor, schema: str, model: type[models.Model]) -> dict[str, list[tuple]]:
    """Everything about a table that publication must carry over unchanged."""
    relation = _relation(schema, model)
    queries = {
        "table": (
            "SELECT relowner::regrole::text, relacl::text, reloptions::text, relpersistence "
            "FROM pg_class WHERE oid = %s::regclass"
        ),
        "columns": (
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "a.attidentity, pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum"
        ),
        "indexes": (
            "SELECT c.relname, pg_get_indexdef(i.indexrelid) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = %s::regclass ORDER BY 1"
        ),
        "constraints": (
            "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass ORDER BY 1"
        ),
        "triggers": (
            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
            "WHERE tgrelid = %s::regclass AND NOT tgisinternal ORDER BY 1"
        ),
        "sequences": (
            "SELECT s.relname FROM pg_depend d JOIN pg_class s ON s.oid = d.objid "
            "WHERE d.refobjid = %s::regclass AND s.relkind = 'S' ORDER BY 1"
        ),
    }
    definition = {}
    for part, query in queries.items():
        cursor.execute(query, [relation])
        definition[part] = [
            tuple(
                _schema_neutral(value, schema) if isinstance(value, str) else value for value in row
            )
            for row in cursor.fetchall()
        ]
    return definition


def _exchangeable(
    cursor, models_list: Sequence[type[models.Model]], county_scoped: frozenset
) -> frozenset[type[models.Model]]:
    """The declared tables publication may exchange whole.

    A table shared by counties keeps row replacement, as does any table joined by a
    foreign key to a table outside the exchanged set or read by a view: those
    references follow the table itself, not its name.
    """
    oids = {}
    for model in models_list:
        cursor.execute("SELECT %s::regclass::oid", [_relation("public", model)])
        oids[cursor.fetchone()[0]] = model
    cursor.execute(
        "SELECT conrelid::oid, confrelid::oid FROM pg_constraint WHERE contype = 'f' "
        "AND (conrelid = ANY(%s) OR confrelid = ANY(%s))",
        [list(oids), list(oids)],
    )
    keys = cursor.fetchall()
    cursor.execute(
        "SELECT DISTINCT d.refobjid::oid FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid "
        "WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = ANY(%s) "
        "AND r.ev_class <> d.refobjid",
        [list(oids)],
    )
    viewed = {oid for (oid,) in cursor.fetchall()}
    exchanged = {oid for oid, model in oids.items() if model not in county_scoped} - viewed
    while crossing := {
        oid
        for source, target in keys
        if (source in exchanged) != (target in exchanged)
        for oid in (source, target)
        if oid in exchanged
    }:
        exchanged -= crossing
    return frozenset(oids[oid] for oid in exchanged)


def _unnamed_index(definition: str) -> str:
    return re.sub(r"^(CREATE (?:UNIQUE )?INDEX )\S+ ON ", r"\1ON ", definition)


def _adopt_production_index_names(cursor, schema: str, model, renames: Iterator[int]) -> None:
    """Rename a staged table's indexes, and the constraints they back, to production's
    names, pairing each with the production index of the same definition."""
    with _qualified_catalog(cursor):
        indexes = {}
        for source in ("public", schema):
            cursor.execute(
                "SELECT c.relname, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c "
                "ON c.oid = i.indexrelid WHERE i.indrelid = %s::regclass ORDER BY 1",
                [_relation(source, model)],
            )
            indexes[source] = [
                (name, _unnamed_index(_schema_neutral(definition, source)))
                for name, definition in cursor.fetchall()
            ]
    unclaimed = list(indexes["public"])
    pairs = []
    for staged, definition in indexes[schema]:
        match = next((name for name, wanted in unclaimed if wanted == definition), None)
        if match is not None:
            unclaimed.remove((match, definition))
            pairs.append((staged, match, f"candidate_index_{next(renames)}"))
    # Every staged index first takes a unique temporary name, so no rename collides.
    quoted_schema = connection.ops.quote_name(schema)
    for staged, _, temporary in pairs:
        cursor.execute(
            f"ALTER INDEX {quoted_schema}.{connection.ops.quote_name(staged)} RENAME TO {temporary}"
        )
    for _, production, temporary in pairs:
        cursor.execute(
            f"ALTER INDEX {quoted_schema}.{temporary} RENAME TO {connection.ops.quote_name(production)}"
        )


def _production_foreign_key_name(cursor, source_model, source_col: str, target_model) -> str | None:
    cursor.execute(
        "SELECT c.conname FROM pg_constraint c JOIN pg_attribute a "
        "ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.contype = 'f' AND c.conrelid = %s::regclass AND c.confrelid = %s::regclass "
        "AND array_length(c.conkey, 1) = 1 AND a.attname = %s",
        [_relation("public", source_model), _relation("public", target_model), source_col],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def drop_candidate_schema(candidate: ImportCandidate) -> None:
    """Drop a retired candidate's staged schema; dropping it again does nothing."""
    validate_schema_name(candidate.storage_schema, county=candidate.county)
    with fenced_write(), connection.cursor() as cursor:
        # A transaction that wrote staged rows cannot drop their tables while its
        # deferred foreign-key checks are pending, so run those checks now.
        connection.check_constraints()
        cursor.execute(
            f"DROP SCHEMA IF EXISTS {connection.ops.quote_name(candidate.storage_schema)} CASCADE"
        )
