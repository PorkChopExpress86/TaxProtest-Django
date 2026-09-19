"""Authorize and bind replay requests; county modules interpret retained inputs."""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied

from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


class ReplayRejected(ValueError):
    pass


@dataclass(frozen=True)
class ReplayRequest:
    candidate_id: UUID
    binding: str
    reason: str
    id: UUID = field(default_factory=uuid4)


def requested_replay(request):
    return (
        {
            "recovery": {
                "request_id": str(request.id),
                "source_candidate_id": str(request.candidate_id),
                "binding": request.binding,
                "reason": request.reason,
            }
        }
        if request
        else {}
    )


def replay_binding(candidate):
    return hashlib.sha256(
        json.dumps(
            {
                "candidate_id": str(candidate.pk),
                "state": candidate.state,
                "request": candidate.request,
                "sources": candidate.sources,
                "content_identity": candidate.evidence.get("content_identity"),
                "property_source_year": candidate.evidence.get("property_source_year"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_replay(request, operation, before):
    candidate = ImportCandidate.objects.get(pk=request.candidate_id, county=operation.county)
    operation.publication_before = before
    operation.evidence["recovery"] = {
        "request_id": str(request.id),
        "source_candidate_id": str(candidate.pk),
        "source_operation_id": str(candidate.operation_id),
        "binding": request.binding,
        "reason": request.reason,
        "request": candidate.request,
        "sources": candidate.sources,
        "property_source_year": candidate.evidence.get("property_source_year"),
    }
    operation.save(update_fields=["publication_before", "evidence"])
    if not request.reason.strip():
        raise ReplayRejected("A recovery reason is required")
    if (
        candidate.state not in ("published", "superseded")
        or not candidate.operation.audit_entries.filter(
            kind="publication", result="published"
        ).exists()
    ):
        raise ReplayRejected("The selected candidate has no observed publication to restore")
    if replay_binding(candidate) != request.binding:
        raise ReplayRejected("Historical source evidence changed; reload and review it again")
    if not candidate.sources:
        raise ReplayRejected("Exact historical sources are unavailable")
    for source in candidate.sources:
        try:
            if _digest(Path(source["path"])) != source["sha256"]:
                raise ReplayRejected(f"Retained source changed: {source['path']}")
        except OSError as exc:
            raise ReplayRejected(f"Retained source unavailable: {source['path']}") from exc
    return candidate


def copy_exact_source(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with Path(source["path"]).open("rb") as reader, destination.open("wb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
            writer.write(chunk)
    if digest.hexdigest() != source["sha256"]:
        raise ReplayRejected("Retained source changed during replay acquisition")


def start_recovery(candidate, *, user, reason, binding):
    if not (
        user.is_authenticated
        and user.is_active
        and user.is_staff
        and user.has_perm("data.recover_import_dataset")
    ):
        raise PermissionDenied("Dataset recovery permission is required")
    if not reason.strip():
        raise ReplayRejected("A recovery reason is required")
    request = ReplayRequest(candidate.pk, binding, reason.strip())
    try:
        if candidate.county == "harris":
            from counties.harris.etl_pipeline.candidate import prepare_recovery, published_identity
        else:
            from counties.brazos.property_import import prepare_recovery, published_identity
        result = prepare_recovery(candidate, request, actor=user.get_username())
        operation = ImportOperation.objects.get(pk=result.operation_id)
    except Exception:
        operation = ImportOperation.objects.filter(
            county=candidate.county, evidence__recovery__request_id=str(request.id)
        ).first()
        if operation is None:
            raise
    operation.publication_after = published_identity()
    operation.save(update_fields=["publication_after"])
    evidence = {
        **operation.evidence["recovery"],
        "new_operation_id": str(operation.pk),
        "candidate_id": operation.evidence.get("candidate_id"),
        "before": operation.publication_before,
        "after": operation.publication_after,
    }
    for target, kind in (
        (operation, "dataset_recovery"),
        (candidate.operation, "recovery_request"),
    ):
        ImportAuditEntry.objects.create(
            operation=target,
            kind=kind,
            actor=user.get_username(),
            reason=reason.strip(),
            result=operation.status,
            evidence=evidence,
        )
    if operation.status == "failed":
        raise ReplayRejected(
            f"Recovery import {operation.pk} failed: {'; '.join(operation.errors)}"
        )
    return operation
