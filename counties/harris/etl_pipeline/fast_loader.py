"""PostgreSQL COPY persistence adapters for Harris translated rows.

Source parsing and business rules live in :mod:`row_reader`. This module only
serializes its typed ``RowResult`` values, adds database-owned metadata, and
writes them efficiently through PostgreSQL COPY.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from django.db import connection, transaction
from django.utils import timezone

from .row_reader import (
    FixtureLookup,
    RowResult,
    RowValue,
    iter_building_rows,
    iter_property_rows,
)

logger = logging.getLogger(__name__)

# COPY uses tab-delimited text; this sentinel marks SQL NULL.
_COPY_NULL = r"\N"


def postgres_backend() -> bool:
    """Return True when the default connection targets PostgreSQL."""
    return connection.vendor == "postgresql"


def _copy_field(value: str) -> str:
    """Escape a string for PostgreSQL COPY text format."""
    # Order matters: backslash first, then the structural characters.
    return (
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    )


def _copy_value(value: RowValue) -> str:
    """Serialize one translated value for PostgreSQL text COPY."""
    if value is None:
        return _COPY_NULL
    if isinstance(value, bool):
        return "t" if value else "f"
    return _copy_field(str(value))


def _quoted_identifier(identifier: str) -> str:
    """Quote a known Django table or column identifier for SQL assembly."""
    return connection.ops.quote_name(identifier)


def _copy_rows(
    rows: Iterable[RowResult],
    *,
    table: str,
    columns: tuple[str, ...],
    staging_table: str,
    metadata: tuple[RowValue, ...],
    truncate: bool,
) -> dict[str, int]:
    """COPY translated rows through a staging table and skip unique conflicts.

    COPY itself cannot express ``ON CONFLICT DO NOTHING``. A temporary staging
    table keeps the streaming COPY performance while the final insert makes
    duplicate-key handling match the ORM adapter and provides accurate counts.
    """
    loaded = 0
    invalid = 0
    skipped = 0
    candidates = 0
    quoted_table = _quoted_identifier(table)
    quoted_staging = _quoted_identifier(staging_table)
    quoted_columns = ", ".join(_quoted_identifier(column) for column in columns)

    def copy_lines() -> Iterator[str]:
        nonlocal candidates, invalid, skipped
        for row in rows:
            if row.skip:
                skipped += 1
                continue
            if row.invalid:
                invalid += 1
                continue
            if tuple(row.field_names) != columns[: len(row.field_names)]:
                raise ValueError("COPY adapter received rows for the wrong field order")
            candidates += 1
            yield "\t".join(_copy_value(value) for value in (*row.values, *metadata)) + "\n"

    with transaction.atomic(), connection.cursor() as cursor:
        if truncate:
            cursor.execute(f"TRUNCATE TABLE {quoted_table} RESTART IDENTITY CASCADE")
        cursor.execute(
            f"CREATE TEMPORARY TABLE {quoted_staging} ON COMMIT DROP AS "
            f"SELECT {quoted_columns} FROM {quoted_table} WHERE FALSE"
        )
        cursor.copy_expert(
            f"COPY {quoted_staging} ({quoted_columns}) FROM STDIN WITH (FORMAT text)",
            _GeneratorIO(copy_lines()),
        )
        cursor.execute(
            f"INSERT INTO {quoted_table} ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM {quoted_staging} ON CONFLICT DO NOTHING"
        )
        loaded = cursor.rowcount
        cursor.execute(f"DROP TABLE {quoted_staging}")

    skipped += candidates - loaded
    return {"loaded": loaded, "invalid": invalid, "skipped": skipped}


# ---------------------------------------------------------------------------
# PropertyRecord (real_acct.txt)
# ---------------------------------------------------------------------------

def copy_load_property_records(filepath: Path, truncate: bool = True) -> dict[str, int]:
    """Translate and COPY-load ``real_acct.txt`` through the shared reader."""
    return copy_load_property_rows(iter_property_rows(filepath), truncate=truncate)


def copy_load_property_rows(rows: Iterable[RowResult], truncate: bool = True) -> dict[str, int]:
    """COPY-load already translated PropertyRecord rows."""
    from ..models import PropertyRecord

    now_iso = timezone.now().isoformat()
    result = _copy_rows(
        rows,
        table=PropertyRecord._meta.db_table,
        columns=(
            "account_number",
            "address",
            "city",
            "zipcode",
            "owner_name",
            "value",
            "assessed_value",
            "building_area",
            "land_area",
            "state_class",
            "is_residential",
            "is_data_ready",
            "street_number",
            "street_name",
            "source_url",
            "parcel_id",
            "created_at",
            "updated_at",
        ),
        staging_table="harris_property_copy_stage",
        metadata=(now_iso, now_iso),
        truncate=truncate,
    )
    logger.info("COPY-loaded %s property rows (skipped %s)", result["loaded"], result["skipped"])
    return {"loaded": result["loaded"], "skipped": result["skipped"]}


# ---------------------------------------------------------------------------
# BuildingDetail (building_res.txt)
# ---------------------------------------------------------------------------

def copy_load_building_details(
    filepath: Path,
    account_map: dict[str, int],
    fixtures_aggregator: FixtureLookup,
    truncate: bool = True,
) -> dict[str, int]:
    """Translate and COPY-load ``building_res.txt`` through the shared reader."""
    return copy_load_building_rows(
        iter_building_rows(filepath, account_map, fixtures_aggregator),
        truncate=truncate,
    )


def copy_load_building_rows(rows: Iterable[RowResult], truncate: bool = True) -> dict[str, int]:
    """COPY-load already translated BuildingDetail rows."""
    from ..models import BuildingDetail

    import_date = timezone.now().isoformat()
    batch_id = timezone.now().strftime("%Y%m%d_%H%M%S")
    result = _copy_rows(
        rows,
        table=BuildingDetail._meta.db_table,
        columns=(
            "property_id",
            "account_number",
            "building_number",
            "building_type",
            "building_style",
            "building_class",
            "quality_code",
            "condition_code",
            "year_built",
            "year_remodeled",
            "effective_year",
            "heat_area",
            "base_area",
            "gross_area",
            "stories",
            "foundation_type",
            "exterior_wall",
            "roof_cover",
            "roof_type",
            "bedrooms",
            "bathrooms",
            "half_baths",
            "fireplaces",
            "is_active",
            "import_date",
            "import_batch_id",
            "created_at",
            "updated_at",
        ),
        staging_table="harris_building_copy_stage",
        metadata=(import_date, batch_id, import_date, import_date),
        truncate=truncate,
    )
    logger.info(
        "COPY-loaded %s building rows (invalid %s, skipped %s)",
        result["loaded"],
        result["invalid"],
        result["skipped"],
    )
    return result


class _GeneratorIO(io.RawIOBase):
    """Adapt a generator of text lines into a file-like object for copy_expert.

    psycopg2's ``copy_expert`` calls ``read(size)`` on its file argument; this
    streams the generator without materializing the whole COPY payload in memory.
    """

    def __init__(self, line_iter: Iterator[str]):
        self._iter = line_iter
        self._buf = ""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunks = [self._buf]
            chunks.extend(self._iter)
            self._buf = ""
            return "".join(chunks).encode("utf-8")

        while len(self._buf) < size:
            try:
                self._buf += next(self._iter)
            except StopIteration:
                break
        chunk, self._buf = self._buf[:size], self._buf[size:]
        return chunk.encode("utf-8")

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n
