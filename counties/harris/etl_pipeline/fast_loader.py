"""Fast COPY-based loaders for the two hot ETL files (real_acct, building_res).

These bypass the row-by-row ``bulk_create`` path.  They consume the same
``RowResult`` generators as the ORM path (``model_loader``) and stream the
typed rows straight into PostgreSQL via ``COPY`` (no model object per row).

The generic loaders in :mod:`counties.harris.etl_pipeline.model_loader` remain the
fallback for non-PostgreSQL backends and for any file without a fast path.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

from django.db import connection, transaction
from django.utils import timezone

from .row_reader import BuildingRow, PropertyRow, RowResult

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


def _s(value: str | None, maxlen: int) -> str:
    """Coerce a raw cell to a trimmed, length-capped COPY string field."""
    if not value:
        return ""
    return _copy_field(value.strip()[:maxlen])


def _int(value: int | None) -> str:
    """Coerce an int to a COPY field (\\N when None)."""
    if value is None:
        return _COPY_NULL
    return str(value)


def _dec(value: float | None) -> str:
    """Coerce a float to a numeric COPY field (\\N when None)."""
    if value is None:
        return _COPY_NULL
    return repr(value)


def _bool(value: bool) -> str:
    """Coerce a bool to a COPY field."""
    return "t" if value else "f"


def _property_fields(row: PropertyRow, now_iso: str) -> list[str]:
    return [
        _s(row.account_number, 20),
        _s(row.address, 255),
        _s(row.city, 100),
        _s(row.zipcode, 20),
        _s(row.owner_name, 255),
        _dec(row.value),
        _dec(row.assessed_value),
        _dec(row.building_area),
        _dec(row.land_area),
        _copy_field(row.state_class),
        _bool(row.is_residential),
        _bool(row.is_data_ready),
        _s(row.street_number, 16),
        _s(row.street_name, 128),
        "",  # source_url — NOT NULL with no source column; matches the ORM path
        "",  # parcel_id — deprecated NOT NULL column, dropped in a follow-up
        _copy_field(now_iso),  # created_at
        _copy_field(now_iso),  # updated_at
    ]


def _building_fields(row: BuildingRow, import_date: str, batch_id: str) -> list[str]:
    return [
        str(row.property_id),
        _s(row.account_number, 20),
        str(row.building_number),
        _s(row.building_type, 10),
        _s(row.building_style, 10),
        _s(row.building_class, 10),
        _s(row.quality_code, 10),
        _s(row.condition_code, 10),
        _int(row.year_built),
        _int(row.year_remodeled),
        _int(row.effective_year),
        _dec(row.heat_area),
        _dec(row.base_area),
        _dec(row.gross_area),
        _dec(row.stories),
        _s(row.foundation_type, 10),
        _s(row.exterior_wall, 10),
        _s(row.roof_cover, 10),
        _s(row.roof_type, 10),
        _int(row.bedrooms),
        _dec(row.bathrooms),
        _int(row.half_baths),
        _int(row.fireplaces),
        _bool(row.is_active),
        _copy_field(import_date),
        _copy_field(batch_id),
        _copy_field(import_date),  # created_at
        _copy_field(import_date),  # updated_at
    ]


def copy_load_property_records(rows: Iterator[RowResult], truncate: bool = True) -> dict[str, int]:
    """Load PropertyRow rows into PropertyRecord via COPY.

    Returns a dict with ``loaded`` / ``skipped`` counts.
    """
    from ..models import PropertyRecord

    table = PropertyRecord._meta.db_table
    # COPY bypasses Django, so NOT NULL columns Django would normally populate
    # (auto_now/auto_now_add timestamps, blank CharField defaults) must be
    # supplied explicitly.
    now_iso = timezone.now().isoformat()
    loaded = 0
    skipped = 0

    def lines() -> Iterator[str]:
        nonlocal loaded, skipped
        for result in rows:
            if result.skip:
                skipped += 1
                continue
            if not isinstance(result.row, PropertyRow):
                continue
            loaded += 1
            yield "\t".join(_property_fields(result.row, now_iso)) + "\n"

    # parcel_id is deprecated and unread, but still NOT NULL until the
    # follow-up migration drops it — COPY bypasses Django, so it must be
    # supplied explicitly or the load fails on the not-null constraint.
    columns = (
        "account_number, address, city, zipcode, owner_name, value, "
        "assessed_value, building_area, land_area, state_class, "
        "is_residential, is_data_ready, street_number, street_name, "
        "source_url, parcel_id, created_at, updated_at"
    )

    with transaction.atomic(), connection.cursor() as cursor:
        if truncate:
            cursor.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
        cursor.copy_expert(
            f'COPY "{table}" ({columns}) FROM STDIN WITH (FORMAT text)',
            _GeneratorIO(lines()),
        )

    logger.info("COPY-loaded %s property records (skipped %s)", loaded, skipped)
    return {"loaded": loaded, "skipped": skipped}


def copy_load_building_details(
    rows: Iterator[RowResult],
    truncate: bool = True,
) -> dict[str, int]:
    """Load BuildingRow rows into BuildingDetail via COPY.

    Rows whose account is not in the residential account map are counted as
    invalid, matching the ORM loader.
    """
    from ..models import BuildingDetail

    table = BuildingDetail._meta.db_table
    import_date = timezone.now().isoformat()
    batch_id = timezone.now().strftime("%Y%m%d_%H%M%S")

    loaded = 0
    invalid = 0
    skipped = 0

    def lines() -> Iterator[str]:
        nonlocal loaded, invalid, skipped
        for result in rows:
            if result.skip:
                skipped += 1
                continue
            if result.invalid:
                invalid += 1
                continue
            if not isinstance(result.row, BuildingRow):
                continue
            loaded += 1
            yield "\t".join(_building_fields(result.row, import_date, batch_id)) + "\n"

    columns = (
        "property_id, account_number, building_number, building_type, "
        "building_style, building_class, quality_code, condition_code, "
        "year_built, year_remodeled, effective_year, heat_area, base_area, "
        "gross_area, stories, foundation_type, exterior_wall, roof_cover, "
        "roof_type, bedrooms, bathrooms, half_baths, fireplaces, is_active, "
        "import_date, import_batch_id, created_at, updated_at"
    )

    with transaction.atomic(), connection.cursor() as cursor:
        if truncate:
            cursor.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
        cursor.copy_expert(
            f'COPY "{table}" ({columns}) FROM STDIN WITH (FORMAT text)',
            _GeneratorIO(lines()),
        )

    logger.info(
        "COPY-loaded %s building records (invalid %s, skipped %s)", loaded, invalid, skipped
    )
    return {"loaded": loaded, "invalid": invalid, "skipped": skipped}


class _GeneratorIO(io.RawIOBase):
    """Adapt a generator of text lines into a file-like object for copy_expert.

    psycopg2's ``copy_expert`` calls ``read(size)`` on its file argument; this
    streams the generator without materializing the whole COPY payload in memory.

    The buffer holds *bytes*, not str, so ``read(size)`` can honour ``size``
    exactly. Buffering characters and encoding the slice afterwards overshoots
    whenever a row contains non-ASCII — HCAD files are read as latin-1, so any
    byte above 0x7F becomes a two-byte UTF-8 sequence — and an oversized return
    breaks ``readinto``'s fixed-size destination buffer.
    """

    def __init__(self, line_iter: Iterator[str]):
        self._iter = line_iter
        self._buf = b""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            chunks = [self._buf]
            chunks.extend(line.encode("utf-8") for line in self._iter)
            self._buf = b""
            return b"".join(chunks)

        while len(self._buf) < size:
            try:
                self._buf += next(self._iter).encode("utf-8")
            except StopIteration:
                break
        chunk, self._buf = self._buf[:size], self._buf[size:]
        return chunk

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n
