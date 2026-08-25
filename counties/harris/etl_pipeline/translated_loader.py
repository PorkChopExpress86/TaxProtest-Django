"""Direct-file entry points for the authoritative Harris translated-row path."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .config import ETLConfig
from .fast_loader import copy_load_property_rows, postgres_backend
from .model_loader import ModelLoader
from .row_reader import RowResult, iter_property_rows


@dataclass(frozen=True)
class TranslatedLoadResult:
    """Outcome of loading one translated source file."""

    records_loaded: int
    records_invalid: int
    records_skipped: int


def _limited_rows(rows: Iterable[RowResult], limit: int | None) -> Iterator[RowResult]:
    if limit is None:
        return iter(rows)
    if limit < 1:
        raise ValueError("limit must be at least one")
    return islice(rows, limit)


def load_property_file(
    config: ETLConfig,
    filepath: Path,
    *,
    truncate: bool,
    batch_size: int,
    limit: int | None = None,
) -> TranslatedLoadResult:
    """Translate and persist a ``real_acct.txt`` file through the modern path."""
    rows = _limited_rows(iter_property_rows(filepath), limit)
    if postgres_backend():
        result = copy_load_property_rows(rows, truncate=truncate)
        return TranslatedLoadResult(
            records_loaded=result["loaded"],
            records_invalid=0,
            records_skipped=result["skipped"],
        )

    loader = ModelLoader(config, batch_size=batch_size)
    result = loader.load_property_records(rows, truncate=truncate)
    if result.error:
        raise RuntimeError(result.error)
    return TranslatedLoadResult(
        records_loaded=result.records_loaded,
        records_invalid=result.records_invalid,
        records_skipped=result.records_skipped,
    )
