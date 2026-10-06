"""County candidate ports: what the shared Candidate lifecycle asks each county.

Each county declares its port in its County registration (``county_registry``).
Common code resolves ports only by county slug and never imports county modules; the
port never parses sources or decides readiness criteria for another county (ADR-0018).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from django.db.models import Model

from counties.common.candidate_staging import compute_dataset_hash
from counties.common.county_registry import registration_for

if TYPE_CHECKING:
    from counties.common.candidate_lifecycle import CandidateLoad
    from counties.common.import_coverage import OutcomePopulation
    from counties.common.models import ImportCandidate, ImportOperation


@dataclass(frozen=True)
class CandidateTables:
    """The tables one county stages, identifies, and cuts over. A declaration only."""

    models: tuple[type[Model], ...]  # parents first; cutover deletes in reverse
    county_scoped: frozenset[type[Model]] = frozenset()  # shared, scoped by county
    # (model, column, referenced model, referenced column) keys added to the staged schema
    deferred_keys: tuple[tuple[type[Model], str, type[Model], str], ...] = ()


class CandidatePort(Protocol):
    tables: CandidateTables

    def published(self) -> dict:
        """Cheap, deterministic facts about the live dataset; a change stops publication."""
        ...

    def outcomes(self, candidate: ImportCandidate | None) -> dict[str, OutcomePopulation]:
        """Outcome populations of the published data, or of ``candidate`` as it claims.

        The caller guarantees the database search path points at the measured tables.
        """
        ...

    def replay(self, source: ImportCandidate, operation: ImportOperation) -> CandidateLoad:
        """A load, set up inside ``operation``, replaying ``source``'s retained sources
        exactly (ADR-0016)."""
        ...


def dataset_identity(county: str, schema: str = "public") -> dict:
    """The content digest of one county's declared tables in ``schema``."""
    tables = registration_for(county).port.tables
    return compute_dataset_hash(
        tables.models,
        schema=schema,
        scope_filters={model: f"AND county = '{county}'" for model in tables.county_scoped},
    )


def published_identity(county: str) -> dict:
    """The identity of the county's live dataset: its content digest plus county facts."""
    return {**dataset_identity(county), **registration_for(county).port.published()}
