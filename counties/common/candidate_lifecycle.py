"""The shared Candidate lifecycle: prepare, qualify, review, publish, and recover.

Every entry point runs inside an Import operation. County behaviour is reached only
through the county candidate port registered for ``operation.county`` (ADR-0018).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from django.db import connection

from counties.common.candidate_ports import (
    CandidateTables,
    dataset_identity,
    port_for,
    published_identity,
)
from counties.common.candidate_staging import cutover_staged_tables, staged_candidate_schema
from counties.common.import_audit import OperationStatus
from counties.common.import_coverage import compare_coverage
from counties.common.import_retention import (
    baseline_sources,
    record_publication,
    retain_baseline_sources,
)
from counties.common.import_review import authorize_publication
from counties.common.import_writers import fenced_write
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


@dataclass(frozen=True)
class Loaded:
    """What a county load wrote into the staged candidate schema."""

    complete: bool
    identity: Mapping[str, Any] = field(default_factory=dict)  # added to candidate.request
    evidence: Mapping[str, Any] = field(default_factory=dict)
    audit: Mapping[str, Any] = field(default_factory=dict)  # county facts for publication audit
    result: Any = None  # the county's own result, handed back to its runner


@dataclass(frozen=True)
class CandidateLoad:
    """One county load, handed to preparation by the county runner."""

    identity: Mapping[str, Any]  # recorded as candidate.request; opaque to common
    carries_published: bool  # the candidate keeps published rows, so inherits their sources
    run: Callable[[ImportCandidate, ImportOperation], Loaded]  # runs in the staged schema


@dataclass(frozen=True)
class Prepared:
    candidate: ImportCandidate
    result: Any  # Loaded.result


def _county_rows(tables: CandidateTables, county: str) -> dict:
    return {model: f" WHERE county = '{county}'" for model in tables.county_scoped}


def prepare(
    operation: ImportOperation,
    load: CandidateLoad,
    *,
    automatic_publication: bool = False,
    user=None,
    reason: str = "",
) -> Prepared:
    """Prepare a candidate from ``load`` without changing published data.

    The candidate records the published baseline, the load's evidence, coverage
    against the published outcome populations, and its content identity, then ends
    prepared, awaiting review, or blocked. A failed load blocks the candidate with its
    error and is raised. Baseline sources are retained and evidence saved either way.
    With ``automatic_publication``, a prepared candidate is published at once.
    """
    if connection.vendor != "postgresql":
        raise ValueError("Durable candidate preparation requires PostgreSQL")
    county = operation.county
    port = port_for(county)
    inherited = baseline_sources(county) if load.carries_published else []
    candidate = ImportCandidate.objects.create(
        county=county,
        operation=operation,
        storage_schema=f"{county}_candidate_{uuid4().hex}",
        baseline=published_identity(county),
        request=dict(load.identity),
        evidence={"publication": "Published data unchanged"},
    )
    operation.evidence["candidate_id"] = str(candidate.pk)
    previous = port.outcomes(None)
    try:
        with staged_candidate_schema(
            county,
            candidate,
            port.tables.models,
            shared_models_scope=_county_rows(port.tables, county),
            foreign_keys=port.tables.deferred_keys,
        ):
            loaded = load.run(candidate, operation)
            candidate.request.update(loaded.identity)
            candidate.evidence.update(loaded.evidence, audit=dict(loaded.audit))
            coverage = None
            if loaded.complete:
                coverage = compare_coverage(previous, port.outcomes(candidate))
                candidate.evidence["coverage"] = coverage
        candidate.evidence["content_identity"] = dataset_identity(county, candidate.storage_schema)
        if coverage is None or coverage["hard_failures"]:
            candidate.state = "blocked"
        elif coverage["requires_review"]:
            candidate.state = "awaiting_review"
        else:
            candidate.state = "prepared"
        operation.status = OperationStatus(candidate.state)
    except Exception as exc:
        candidate.state = "blocked"
        candidate.evidence["error"] = str(exc)
        raise
    finally:
        retain_baseline_sources(operation, inherited)
        candidate.sources = operation.evidence.get("sources", [])
        operation.save()
        candidate.save()
    if automatic_publication and candidate.state == "prepared":
        candidate = publish(operation, candidate.pk, user=user, reason=reason)
    return Prepared(candidate, loaded.result)


def publish(
    operation: ImportOperation, candidate_id, *, user=None, reason: str = ""
) -> ImportCandidate:
    """Publish a qualified candidate atomically under the operation's writer reservation.

    Publication rechecks authorization and exact evidence first; any failure rolls the
    whole cutover and its audit back, leaving the previous dataset published. A
    candidate that is already published is reported as already applied.
    """
    county = operation.county
    tables = port_for(county).tables
    operation.evidence["application_reason"] = reason
    with fenced_write():
        candidate = ImportCandidate.objects.select_for_update().get(pk=candidate_id, county=county)
        if candidate.state in ("published", "superseded"):
            operation.publication_before = operation.publication_after = published_identity(county)
            operation.evidence["already_applied"] = str(candidate.pk)
            operation.status = OperationStatus.ALREADY_APPLIED
            return candidate
        review = authorize_publication(candidate, user=user)
        operation.publication_before = published_identity(county)
        cutover_staged_tables(
            candidate,
            tables.models,
            shared_models_scope=_county_rows(tables, county),
            shared_models_county={model: county for model in tables.county_scoped},
        )
        record_publication(candidate, operation)
        operation.publication_after = {
            **published_identity(county),
            "candidate_id": str(candidate.pk),
        }
        operation.status = OperationStatus.PUBLISHED
        operation.evidence.update(
            candidate_id=str(candidate.pk), qualified_publication="Observed atomic publication"
        )
        operation.save()
        ImportAuditEntry.objects.create(
            operation=candidate.operation,
            kind="publication",
            actor=operation.actor,
            reason=reason or "Qualified candidate applied",
            evidence={
                "before": operation.publication_before,
                "after": operation.publication_after,
                "review_id": str(review.pk) if review else None,
                "operation_id": str(operation.pk),
                "source_years": candidate.sources,
                "county": candidate.evidence.get("audit", {}),
            },
            result="published",
        )
    return candidate
