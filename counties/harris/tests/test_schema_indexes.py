"""Index coverage on the Harris building and extra-feature tables (#154).

A plain index whose columns are a leading prefix of a unique constraint's index
duplicates it and slows every staging clone and cutover insert. The lookups
those plain indexes served must still be index-backed.
"""

from __future__ import annotations

from django.db import connection
from django.test import TestCase

from counties.harris.models import BuildingDetail, ExtraFeature


def _indexes(table: str) -> list[tuple[str, bool, list[str]]]:
    """Return (name, is_unique, ordered columns) for every index on ``table``."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT i.relname, ix.indisunique,
                   array_agg(a.attname ORDER BY k.ord)
            FROM pg_index ix
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_class i ON i.oid = ix.indexrelid
            CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE t.relname = %s AND t.relnamespace = to_regnamespace(current_schema())
            GROUP BY i.relname, ix.indisunique
            """,
            [table],
        )
        return [(name, unique, list(cols)) for name, unique, cols in cursor.fetchall()]


def _plan(sql: str, params: list[object]) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute(f"EXPLAIN {sql}", params)
        return "\n".join(row[0] for row in cursor.fetchall())


class RedundantIndexTests(TestCase):
    def test_unique_constraint_prefixes_have_no_duplicate_plain_index(self) -> None:
        duplicated_prefixes = {
            BuildingDetail: ["account_number", "building_number"],
            ExtraFeature: ["account_number", "feature_code"],
        }
        for model, columns in duplicated_prefixes.items():
            plain = [cols for _, unique, cols in _indexes(model._meta.db_table) if not unique]
            with self.subTest(table=model._meta.db_table):
                self.assertNotIn(columns, plain)

    def test_unique_constraints_are_unchanged(self) -> None:
        expected = {
            BuildingDetail: ["account_number", "building_number"],
            ExtraFeature: ["account_number", "feature_code", "feature_number", "source_ordinal"],
        }
        for model, columns in expected.items():
            with self.subTest(table=model._meta.db_table):
                self.assertIn(
                    columns,
                    [cols for _, unique, cols in _indexes(model._meta.db_table) if unique],
                )

    def test_lookups_on_former_index_columns_still_use_an_index(self) -> None:
        lookups = [
            (
                f"SELECT * FROM {BuildingDetail._meta.db_table} "
                "WHERE account_number = %s AND building_number = %s",
                ["0000000000001", 1],
            ),
            (
                f"SELECT * FROM {ExtraFeature._meta.db_table} "
                "WHERE account_number = %s AND feature_code = %s",
                ["0000000000001", "RG1"],
            ),
        ]
        for sql, params in lookups:
            with self.subTest(sql=sql):
                plan = _plan(sql, params)
                self.assertRegex(plan, r"Index (Only )?Scan|Bitmap Index Scan", plan)
