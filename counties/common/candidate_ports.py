"""County candidate ports: what the shared Candidate lifecycle asks each county.

Each county registers one port from its ``AppConfig.ready()``. Common code resolves
ports only by county slug and never imports county modules; the port never parses
sources or decides readiness criteria for another county (ADR-0018).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from django.db.models import Model

from counties.common.candidate_staging import compute_dataset_hash

if TYPE_CHECKING:
    from counties.common.import_coverage import OutcomePopulation
    from counties.common.models import ImportCandidate


class UnknownCountyCandidate(LookupError):
    """No candidate port is registered for the county."""


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


_PORTS: dict[str, CandidatePort] = {}


def register(county: str, port: CandidatePort) -> None:
    _PORTS[county] = port


def port_for(county: str) -> CandidatePort:
    try:
        return _PORTS[county]
    except KeyError:
        raise UnknownCountyCandidate(f"Unknown county candidate: {county}") from None


def dataset_identity(county: str, schema: str = "public") -> dict:
    """The content digest of one county's declared tables in ``schema``."""
    tables = port_for(county).tables
    return compute_dataset_hash(
        tables.models,
        schema=schema,
        scope_filters={model: f"AND county = '{county}'" for model in tables.county_scoped},
    )


def published_identity(county: str) -> dict:
    """The identity of the county's live dataset: its content digest plus county facts."""
    return {**dataset_identity(county), **port_for(county).published()}
