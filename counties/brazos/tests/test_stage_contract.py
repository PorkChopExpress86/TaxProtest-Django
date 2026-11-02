"""The Brazos stage contract lives beside the stages, not inside the orchestrator."""

from __future__ import annotations

import ast
from dataclasses import is_dataclass
from pathlib import Path

from django.test import SimpleTestCase

BRAZOS = Path(__file__).resolve().parents[1]

# Production modules on the property-import path. Tests may reach private helpers.
PRODUCTION_MODULES = (
    "stage_contract.py",
    "property_import.py",
    "cad_refresh.py",
    "gis_refresh.py",
    "source_validation.py",
    "candidate.py",
)


def _brazos_imports_inside_functions(tree: ast.AST) -> list[ast.stmt]:
    found = []
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(scope):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "counties.brazos"
                ):
                    found.append(node)
                elif isinstance(node, ast.Import) and any(
                    alias.name.startswith("counties.brazos") for alias in node.names
                ):
                    found.append(node)
    return found


def _private_brazos_names(tree: ast.AST) -> list[str]:
    return [
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("counties.brazos")
        for alias in node.names
        if alias.name.startswith("_")
    ]


CONTRACT_NAMES = (
    "RefreshOptions",
    "StagePreparation",
    "StageResult",
    "PropertyImportMode",
    "PropertyImportRequest",
    "PropertyImportResult",
    "PropertyImportStage",
    "CadStagePayload",
    "GisSourcePayload",
)
# Names the orchestrator keeps exporting so existing imports and tests keep working.
REEXPORTED_NAMES = (
    "RefreshOptions",
    "StagePreparation",
    "StageResult",
    "PropertyImportMode",
    "PropertyImportRequest",
    "PropertyImportResult",
    "PropertyImportStage",
    "PropertyImportOutcome",
    "AnnualRefreshStage",
)


class StageContractModuleTests(SimpleTestCase):
    def test_contract_types_are_defined_in_the_stage_contract_module(self):
        from counties.brazos import property_import, stage_contract

        for name in CONTRACT_NAMES:
            with self.subTest(name=name):
                self.assertEqual(getattr(stage_contract, name).__module__, stage_contract.__name__)

        for name in REEXPORTED_NAMES:
            with self.subTest(name=name):
                # The orchestrator re-exports the same object, so existing imports keep working.
                self.assertIs(getattr(property_import, name), getattr(stage_contract, name))

    def test_replay_payload_is_public_in_memory_state(self):
        from counties.brazos import cad_refresh
        from counties.brazos.stage_contract import CadStagePayload

        self.assertTrue(is_dataclass(CadStagePayload))
        self.assertFalse(hasattr(cad_refresh, "_CadStagePayload"))


class NoLazyOrPrivateBrazosImportsTests(SimpleTestCase):
    def test_production_modules_import_brazos_modules_at_module_level(self):
        for module in PRODUCTION_MODULES:
            with self.subTest(module=module):
                tree = ast.parse((BRAZOS / module).read_text(encoding="utf-8"))
                lazy = [f"line {node.lineno}" for node in _brazos_imports_inside_functions(tree)]
                self.assertEqual(lazy, [])

    def test_production_modules_import_no_private_names_from_brazos_modules(self):
        for module in PRODUCTION_MODULES:
            with self.subTest(module=module):
                tree = ast.parse((BRAZOS / module).read_text(encoding="utf-8"))
                self.assertEqual(_private_brazos_names(tree), [])
