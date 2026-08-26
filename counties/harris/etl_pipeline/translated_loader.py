"""Direct-file entry points for the authoritative Harris translated-row path."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .config import ETLConfig
from .persistence import (
    PersistenceDataset,
    PersistenceRequest,
    PersistenceWriteMode,
    persistence_for_connection,
)
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
    """Translate and persist a ``real_acct.txt`` file through the modern path.

    ``config`` remains in this focused loader's public signature for command
    compatibility. Connection-specific persistence selection now belongs to
    :func:`persistence_for_connection`.
    """
    rows = _limited_rows(iter_property_rows(filepath), limit)
    result = persistence_for_connection(orm_batch_size=batch_size).persist(
        PersistenceRequest(
            dataset=PersistenceDataset.PROPERTY,
            rows=rows,
            write_mode=(
                PersistenceWriteMode.REPLACE if truncate else PersistenceWriteMode.ADD_MISSING
            ),
        )
    )
    return TranslatedLoadResult(
        records_loaded=result.loaded,
        records_invalid=result.invalid,
        records_skipped=result.skipped,
    )
