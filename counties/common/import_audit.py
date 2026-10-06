"""Audit/ownership primitives; county callers select and interpret their sources."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from django.utils import timezone

from counties.common.county_registry import registration_for
from counties.common.import_logging import import_warnings
from counties.common.import_writers import county_writer
from counties.common.models import ImportOperation


def record_sources(operation: ImportOperation, paths: list[Path], **identity) -> None:
    sources = operation.evidence.setdefault("sources", [])
    for path in paths:
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        sources.append({"path": str(path), "sha256": digest.hexdigest(), **identity})


class OperationStatus(StrEnum):
    """The one status vocabulary of an Import operation."""

    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PARTIAL = "partial"
    PREPARED = "prepared"
    BLOCKED = "blocked"
    AWAITING_REVIEW = "awaiting_review"
    PUBLISHED = "published"
    ALREADY_APPLIED = "already_applied"
    FAILED = "failed"


@contextmanager
def audited_operation(
    county: str,
    intent: str,
    *,
    actor: str = "",
    origin: str = "command",
    requested_year: int | None = None,
    evidence: dict | None = None,
    publication_before: dict | None = None,
) -> Iterator[ImportOperation]:
    """Run one Import operation under the county writer reservation.

    The caller may classify ``operation.status`` before the block ends; otherwise it
    completes. A failure after a committed publication is recorded as a warning and
    not raised, because the published dataset is live. Any other failure fails the
    operation and is raised.
    """
    registration = registration_for(county)  # an unknown county records no operation
    operation = ImportOperation.objects.create(
        county=county,
        intent=intent,
        actor=actor,
        origin=origin,
        requested_year=requested_year,
        evidence=evidence or {},
        publication_before=publication_before,
    )
    warnings: list[str] = []
    try:
        with (
            import_warnings(
                registration.warning_logger, operation_id=str(operation.pk)
            ) as warnings,
            county_writer(operation),
        ):
            yield operation
        if operation.status == OperationStatus.RUNNING:
            operation.status = (
                OperationStatus.COMPLETED_WITH_WARNINGS
                if operation.warnings
                else OperationStatus.COMPLETED
            )
        operation.status = OperationStatus(operation.status)
    except Exception as exc:
        if ImportOperation.objects.filter(
            pk=operation.pk, status=OperationStatus.PUBLISHED
        ).exists():
            operation.refresh_from_db(fields=["status", "publication_before", "publication_after"])
            operation.warnings.append(str(exc))
            operation.evidence["post_publication_failure"] = str(exc)
            return
        if operation.status == OperationStatus.COMPLETED_WITH_WARNINGS:
            operation.warnings.append(str(exc))
        else:
            # Publication values recorded by a rolled-back transaction were never observed.
            operation.refresh_from_db(fields=["publication_before", "publication_after"])
            operation.status = OperationStatus.FAILED
            operation.errors.append(str(exc))
        raise
    finally:
        operation.warnings = list(dict.fromkeys([*operation.warnings, *warnings]))
        operation.finished_at = timezone.now()
        operation.save()
