"""Distributed lock preventing concurrent ETL pipeline runs.

ETLOrchestrator.execute() truncates and bulk-COPYs into shared tables
(see model_loader.py / fast_loader.py). Two runs executing at once — an
admin-triggered task overlapping a Celery Beat schedule, or a manually
run `import_all_data` overlapping either — can race on that truncate-then-load
sequence and corrupt data. Redis is already the shared coordination point
between the Django web process and Celery workers (it's the Celery broker),
so it's reused here rather than introducing a second shared store.

Failures talking to Redis are intentionally NOT swallowed: a lock that
silently fails open defeats the point of it.
"""

from __future__ import annotations

import json
import time
from typing import Any

import redis
from django.conf import settings

LOCK_KEY = "etl:pipeline:lock"
LOCK_TTL_SECONDS = 4 * 60 * 60  # dead-worker safety net; longest documented run is ~30min


class PipelineAlreadyRunningError(RuntimeError):
    """Raised when an ETL run is attempted while another is already in progress."""


def _client() -> redis.Redis:
    return redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=2)


def acquire(scope: str, task_id: str | None) -> None:
    """Acquire the global ETL pipeline lock or raise PipelineAlreadyRunningError."""
    client = _client()
    payload = json.dumps({"scope": scope, "task_id": task_id, "started_at": time.time()})
    if not client.set(LOCK_KEY, payload, nx=True, ex=LOCK_TTL_SECONDS):
        held_by = current_run() or {}
        raise PipelineAlreadyRunningError(
            f"An ETL pipeline run (scope={held_by.get('scope', 'unknown')}) is already in progress."
        )


def release() -> None:
    """Release the global ETL pipeline lock."""
    _client().delete(LOCK_KEY)


def current_run() -> dict[str, Any] | None:
    """Return {'scope', 'task_id', 'started_at'} for the in-progress run, or None."""
    raw = _client().get(LOCK_KEY)
    if raw is None:
        return None
    return json.loads(raw)
