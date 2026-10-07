"""No module, test or production, imports a private similarity name (ADR-0021).

Tests exercise similarity through each county's public search and the shared
math's public functions, so they survive refactors of the county scorers.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

SIMILARITY_MODULES = {
    "counties.brazos.similarity",
    "counties.harris.similarity",
    "counties.common.similarity_math",
    "counties.common.tests.similarity_scenarios",
}


class SimilarityPrivateNameBoundaryTests(SimpleTestCase):
    def test_no_module_imports_a_private_similarity_name(self) -> None:
        root = Path(__file__).resolve().parents[3]
        offenders = []
        for package in ("counties", "taxprotest"):
            for path in (root / package).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module in SIMILARITY_MODULES:
                        offenders.extend(
                            f"{path.relative_to(root)}: {node.module}.{alias.name}"
                            for alias in node.names
                            if alias.name.startswith("_")
                        )
        self.assertEqual(offenders, [])
