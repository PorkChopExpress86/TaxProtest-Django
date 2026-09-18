"""Audit/ownership primitives; county callers select and interpret their sources."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.utils import timezone

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


@contextmanager
def audited_operation(
    county: str,
    intent: str,
    *,
    actor: str = "",
    origin: str = "command",
    requested_year: int | None = None,
) -> Iterator[ImportOperation]:
    operation = ImportOperation.objects.create(
        county=county, intent=intent, actor=actor, origin=origin, requested_year=requested_year
    )
    try:
        with (
            import_warnings(
                "brazos_cad" if county == "brazos" else "etl_orchestrator",
                operation_id=str(operation.pk),
            ) as warnings,
            county_writer(operation),
        ):
            yield operation
        operation.status = "completed_with_warnings" if operation.warnings else "completed"
    except Exception as exc:
        if operation.status == "completed_with_warnings":
            operation.warnings.append(str(exc))
        else:
            operation.status = "failed"
            operation.errors.append(str(exc))
        raise
    finally:
        operation.warnings = list(dict.fromkeys([*operation.warnings, *warnings]))
        operation.finished_at = timezone.now()
        operation.save()
