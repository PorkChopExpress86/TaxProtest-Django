"""Harris import intent, source selection, and completeness expectations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from counties.harris.source_catalog import (
    DEFAULT_HCAD_SOURCE_CATALOG,
    DataSource,
    HarrisImportStage,
)

ALL_IMPORT_STAGES = frozenset(HarrisImportStage)


@dataclass(frozen=True)
class HarrisImportPlan:
    """Describe which Harris stages a caller intends to refresh.

    The plan owns source selection and validation skips, so command flags and
    Celery callers cannot independently encode a contradictory execution scope.
    """

    stages: frozenset[HarrisImportStage]

    def __post_init__(self) -> None:
        if not self.stages:
            raise ValueError("A Harris import plan must select at least one stage")
        unknown = self.stages.difference(ALL_IMPORT_STAGES)
        if unknown:
            raise ValueError(f"Unsupported Harris import stages: {sorted(unknown)}")

    @classmethod
    def from_stage_flags(
        cls,
        *,
        include_property: bool,
        include_building: bool,
        include_gis: bool,
    ) -> HarrisImportPlan:
        return cls(
            frozenset(
                stage
                for stage, included in {
                    HarrisImportStage.PROPERTY: include_property,
                    HarrisImportStage.BUILDING: include_building,
                    HarrisImportStage.GIS: include_gis,
                }.items()
                if included
            )
        )

    @classmethod
    def from_legacy_scope(cls, scope: str) -> HarrisImportPlan:
        try:
            stages = _STAGES_BY_LEGACY_SCOPE[scope]
        except KeyError as exc:
            raise ValueError(f"Unsupported pipeline scope: {scope}") from exc
        return cls(stages)

    @property
    def legacy_scope(self) -> str:
        return _LEGACY_SCOPE_BY_STAGES[self.stages]

    @property
    def is_full(self) -> bool:
        return self.stages == ALL_IMPORT_STAGES

    def select_sources(self, sources: Iterable[DataSource]) -> list[DataSource]:
        """Return sources relevant to this plan, preserving the caller's order."""
        selected: list[DataSource] = []
        for source in sources:
            source_stages = DEFAULT_HCAD_SOURCE_CATALOG.import_stages_for(source)
            if source_stages.intersection(self.stages):
                selected.append(source)
            elif self.is_full and not source_stages:
                selected.append(source)
        return selected

    def contract_validation_options(self) -> dict[str, bool]:
        """Return validate_data skip flags consistent with the requested stages."""
        return {
            "skip_building_checks": HarrisImportStage.BUILDING not in self.stages,
            "skip_gis_checks": HarrisImportStage.GIS not in self.stages,
        }


_STAGES_BY_LEGACY_SCOPE: dict[str, frozenset[HarrisImportStage]] = {
    "full": ALL_IMPORT_STAGES,
    "property-only": frozenset({HarrisImportStage.PROPERTY}),
    "building-only": frozenset({HarrisImportStage.BUILDING}),
    "gis-only": frozenset({HarrisImportStage.GIS}),
    "property-and-building": frozenset({HarrisImportStage.PROPERTY, HarrisImportStage.BUILDING}),
    "property-and-gis": frozenset({HarrisImportStage.PROPERTY, HarrisImportStage.GIS}),
    "building-and-gis": frozenset({HarrisImportStage.BUILDING, HarrisImportStage.GIS}),
}
_LEGACY_SCOPE_BY_STAGES = {stages: scope for scope, stages in _STAGES_BY_LEGACY_SCOPE.items()}


__all__ = ["ALL_IMPORT_STAGES", "HarrisImportPlan"]
