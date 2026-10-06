"""County-neutral harness for the operator ETL benchmark commands (ADR-0017, ADR-0022).

Each county owns its benchmark body, synthetic-data builder and management command; this
module only times a run, measures its peak memory, discards the candidate it prepared and
prints the result. It never names a county or imports county code.
"""

from __future__ import annotations

import time
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from counties.common.models import ImportCandidate

R = TypeVar("R")
_ABSENT = object()


@dataclass(frozen=True)
class BenchmarkStats:
    county: str
    rows: int
    duration: float
    peak_memory_mb: float
    status: str

    @property
    def throughput(self) -> float:
        return self.rows / self.duration if self.duration > 0 else 0.0


@contextmanager
def swapped_settings(**values: Any) -> Iterator[None]:
    """Point runtime settings at benchmark directories, restoring them afterwards."""
    previous = {name: getattr(settings, name, _ABSENT) for name in values}
    for name, value in values.items():
        setattr(settings, name, value)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is _ABSENT:
                delattr(settings, name)
            else:
                setattr(settings, name, value)


def _discard_candidate(candidate_id) -> None:
    candidate = ImportCandidate.objects.filter(pk=candidate_id).first()
    if candidate is None:
        return
    with connection.cursor() as cursor:
        cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
    candidate.delete()


def measure(
    county: str, *, rows: int, run: Callable[[], R], status: Callable[[R], str]
) -> BenchmarkStats:
    """Time ``run`` and its peak memory, then discard the candidate it prepared.

    ``run`` returns a county import result carrying ``candidate_id``; ``status`` reads the
    county's own result field for display.
    """
    tracemalloc.start()
    start_time = time.monotonic()
    try:
        result = run()
    finally:
        duration = time.monotonic() - start_time
        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    candidate_id = getattr(result, "candidate_id", None)
    if candidate_id:
        _discard_candidate(candidate_id)
    return BenchmarkStats(
        county=county,
        rows=rows,
        duration=duration,
        peak_memory_mb=peak_mem / (1024 * 1024),
        status=status(result),
    )


class BenchmarkCommand(BaseCommand):
    """Base for a county's benchmark command; the county supplies ``county`` and ``benchmark``."""

    county: str

    help = "Benchmark county ETL candidate staging throughput and memory consumption."

    def benchmark(self, rows: int) -> BenchmarkStats:
        raise NotImplementedError

    def add_arguments(self, parser):
        parser.add_argument(
            "--rows",
            type=int,
            default=5000,
            help="Number of synthetic records to stage (default: 5000)",
        )

    def handle(self, *args, **options):
        rows = options["rows"]
        if rows <= 0:
            raise CommandError("Number of rows must be positive")

        self.stdout.write(f"Starting pipeline benchmark for {self.county} with {rows} rows...")
        stats = self.benchmark(rows)

        self.stdout.write(self.style.SUCCESS("Benchmark completed successfully:"))
        self.stdout.write(f"  County:      {stats.county}")
        self.stdout.write(f"  Rows:        {stats.rows}")
        self.stdout.write(f"  Duration:    {stats.duration:.2f} s")
        self.stdout.write(f"  Throughput:  {stats.throughput:.1f} rows/s")
        self.stdout.write(f"  Peak Memory: {stats.peak_memory_mb:.2f} MB")
        self.stdout.write(f"  Status:      {stats.status}")
