"""Index coverage on the Harris building and extra-feature tables (#154).

A plain index whose columns are a leading prefix of a unique constraint's index
duplicates it and slows every staging clone and cutover insert. The lookups
those plain indexes served must still be index-backed.
"""

from __future__ import annotations

from django.db import connection, models
from django.test import TestCase

from counties.harris.models import BuildingDetail, ExtraFeature


def _index_columns(model: type[models.Model], *, unique: bool) -> list[list[str]]:
    """Return the ordered columns of each unique, or each plain, index on ``model``."""
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, model._meta.db_table)
    if unique:
        return [c["columns"] for c in constraints.values() if c["unique"]]
    return [c["columns"] for c in constraints.values() if c["index"] and not c["unique"]]


def _query_plan(sql: str, params: list[object]) -> str:
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
            with self.subTest(table=model._meta.db_table):
                self.assertNotIn(columns, _index_columns(model, unique=False))

    def test_unique_constraints_are_unchanged(self) -> None:
        expected = {
            BuildingDetail: ["account_number", "building_number"],
            ExtraFeature: ["account_number", "feature_code", "feature_number", "source_ordinal"],
        }
        for model, columns in expected.items():
            with self.subTest(table=model._meta.db_table):
                self.assertIn(columns, _index_columns(model, unique=True))

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
                plan = _query_plan(sql, params)
                self.assertRegex(plan, r"Index (Only )?Scan|Bitmap Index Scan", plan)
