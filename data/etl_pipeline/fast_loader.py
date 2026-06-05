"""Fast COPY-based loaders for the two hot ETL files (real_acct, building_res).

These bypass the generic ``csv.DictReader`` + ``transform_row`` path (which
rebuilds a column-name lookup for every field of every row) and the row-by-row
``bulk_create``. Instead they:

1. Resolve source-column -> index ONCE from the header line.
2. Read positionally with ``csv.reader`` (no per-row dict construction).
3. Stream rows straight into PostgreSQL via ``COPY`` (no model object per row).

The generic loaders in :mod:`data.etl_pipeline.model_loader` remain the
fallback for non-PostgreSQL backends and for any file without a fast path.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from data.residential import is_residential_state_class, normalize_state_class

logger = logging.getLogger(__name__)

# COPY uses tab-delimited text; this sentinel marks SQL NULL.
_COPY_NULL = r"\N"


def postgres_backend() -> bool:
    """Return True when the default connection targets PostgreSQL."""
    return connection.vendor == "postgresql"


def _resolve_indices(header: list[str], source_names: dict[str, list[str]]) -> dict[str, int]:
    """Map each logical field to its column index using the header row.

    ``source_names`` maps a logical field name to the candidate source columns
    (first match wins, case-insensitive), mirroring ``FieldSchema.get_source_name``.
    """
    lower_to_idx = {name.lower(): i for i, name in enumerate(header) if name is not None}
    indices: dict[str, int] = {}
    for field_name, candidates in source_names.items():
        for cand in candidates:
            idx = lower_to_idx.get(cand.lower())
            if idx is not None:
                indices[field_name] = idx
                break
    return indices


def _open_text(filepath: Path) -> tuple[csv.reader, Any]:
    """Open a HCAD data file as a tab-delimited positional csv.reader."""
    # HCAD files are latin-1/tab-delimited; errors="ignore" matches the
    # generic transformer's tolerance for stray bytes.
    fh = open(filepath, encoding="latin-1", errors="ignore", newline="")
    reader = csv.reader(fh, delimiter="\t")
    return reader, fh


def _copy_field(value: str) -> str:
    """Escape a string for PostgreSQL COPY text format."""
    # Order matters: backslash first, then the structural characters.
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _s(value: str | None, maxlen: int) -> str:
    """Coerce a raw cell to a trimmed, length-capped COPY string field."""
    if not value:
        return ""
    return _copy_field(value.strip()[:maxlen])


def _int(value: str | None) -> str:
    """Coerce a raw cell to an int COPY field (\\N when empty/invalid)."""
    if not value:
        return _COPY_NULL
    try:
        return str(int(float(value.strip())))
    except (ValueError, TypeError):
        return _COPY_NULL


def _dec(value: str | None) -> str:
    """Coerce a raw cell to a numeric COPY field (\\N when empty/invalid)."""
    if not value:
        return _COPY_NULL
    v = value.strip().replace("$", "").replace(",", "")
    if not v:
        return _COPY_NULL
    try:
        return repr(float(v))
    except (ValueError, TypeError):
        return _COPY_NULL


# ---------------------------------------------------------------------------
# PropertyRecord (real_acct.txt)
# ---------------------------------------------------------------------------

_REAL_ACCT_SOURCES = {
    "account_number": ["acct", "account_num", "account"],
    "owner_name": ["mailto", "owner_name", "owner"],
    "street_number": ["str_num", "site_addr_num"],
    "street_name": ["str", "site_addr_street"],
    "street_suffix": ["str_sfx"],
    "site_addr_1": ["site_addr_1", "site_addr"],
    "city": ["site_addr_2", "situs_city", "city"],
    "zipcode": ["site_addr_3", "zip", "zip_code"],
    "state_class": ["state_class"],
    "value": ["tot_appr_val", "mkt_val"],
    "assessed_value": ["assessed_val"],
    "building_area": ["bld_ar", "bldg_ar"],
    "land_area": ["land_ar"],
}


def copy_load_property_records(filepath: Path, truncate: bool = True) -> dict[str, int]:
    """Load PropertyRecord rows from real_acct.txt via COPY.

    Returns a dict with ``loaded`` / ``skipped`` counts. Only residential rows
    with a non-empty account number are loaded, matching the generic loader.
    """
    from data.models import PropertyRecord

    table = PropertyRecord._meta.db_table
    # COPY bypasses Django, so NOT NULL columns Django would normally populate
    # (auto_now/auto_now_add timestamps, blank CharField defaults) must be
    # supplied explicitly.
    now_iso = timezone.now().isoformat()
    reader, fh = _open_text(filepath)
    loaded = 0
    skipped = 0

    try:
        header = next(reader, None)
        if header is None:
            return {"loaded": 0, "skipped": 0}
        idx = _resolve_indices(header, _REAL_ACCT_SOURCES)

        def get(row: list[str], field: str) -> str | None:
            i = idx.get(field)
            if i is None or i >= len(row):
                return None
            return row[i]

        def rows() -> Iterator[str]:
            nonlocal loaded, skipped
            for row in reader:
                acct = (get(row, "account_number") or "").strip()
                if not acct:
                    skipped += 1
                    continue

                state_class = normalize_state_class(get(row, "state_class"))[:10]
                if not is_residential_state_class(state_class):
                    skipped += 1
                    continue

                street_num = (get(row, "street_number") or "").strip()
                street_name_base = (get(row, "street_name") or "").strip()
                street_suffix = (get(row, "street_suffix") or "").strip()
                street_name = (
                    f"{street_name_base} {street_suffix}".strip()
                    if street_suffix
                    else street_name_base
                )
                site_addr = (get(row, "site_addr_1") or "").strip()
                address = site_addr or f"{street_num} {street_name}".strip()

                fields = [
                    _s(acct, 20),
                    _s(address, 255),
                    _s(get(row, "city"), 100),
                    _s(get(row, "zipcode"), 20),
                    _s(get(row, "owner_name"), 255),
                    _dec(get(row, "value")),
                    _dec(get(row, "assessed_value")),
                    _dec(get(row, "building_area")),
                    _dec(get(row, "land_area")),
                    _copy_field(state_class),
                    "t",  # is_residential
                    "f",  # is_data_ready
                    _s(street_num, 16),
                    _s(street_name, 128),
                    "",  # source_url (NOT NULL, blank default)
                    "",  # parcel_id (NOT NULL, blank default; GIS load fills later)
                    _copy_field(now_iso),  # created_at
                    _copy_field(now_iso),  # updated_at
                ]
                loaded += 1
                yield "\t".join(fields) + "\n"

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
                _GeneratorIO(rows()),
            )
    finally:
        fh.close()

    logger.info("COPY-loaded %s property records (skipped %s)", loaded, skipped)
    return {"loaded": loaded, "skipped": skipped}


# ---------------------------------------------------------------------------
# BuildingDetail (building_res.txt)
# ---------------------------------------------------------------------------

_BUILDING_RES_SOURCES = {
    "account_number": ["acct"],
    "building_number": ["bld_num"],
    "building_type": ["imprv_type"],
    "building_style": ["building_style_code"],
    "building_class": ["bldg_class"],
    "quality_code": ["qa_cd"],
    "condition_code": ["cndtn_cd"],
    "year_built": ["date_erected"],
    "year_remodeled": ["yr_remodel"],
    "effective_year": ["eff_yr"],
    "heat_area": ["heat_ar"],
    "base_area": ["base_ar"],
    "gross_area": ["gross_ar"],
    "stories": ["sty"],
    "foundation_type": ["foundation"],
    "exterior_wall": ["exterior_wall"],
    "roof_cover": ["roof_cover"],
    "roof_type": ["roof_typ"],
    "bedrooms": ["bed_rm"],
    "full_baths": ["full_bath"],
    "half_baths": ["half_bath"],
    "fireplaces": ["fireplace"],
}


def copy_load_building_details(
    filepath: Path,
    account_map: dict[str, int],
    fixtures_aggregator: Any,
    truncate: bool = True,
) -> dict[str, int]:
    """Load BuildingDetail rows from building_res.txt via COPY.

    ``account_map`` maps account numbers to PropertyRecord ids (residential
    only). ``fixtures_aggregator`` supplies bedroom/bathroom counts pre-loaded
    from fixtures.txt. Rows whose account is not in ``account_map`` are counted
    as invalid, matching the generic loader.
    """
    from data.models import BuildingDetail

    table = BuildingDetail._meta.db_table
    import_date = timezone.now().isoformat()
    batch_id = timezone.now().strftime("%Y%m%d_%H%M%S")

    reader, fh = _open_text(filepath)
    loaded = 0
    invalid = 0
    skipped = 0

    try:
        header = next(reader, None)
        if header is None:
            return {"loaded": 0, "invalid": 0, "skipped": 0}
        idx = _resolve_indices(header, _BUILDING_RES_SOURCES)

        def get(row: list[str], field: str) -> str | None:
            i = idx.get(field)
            if i is None or i >= len(row):
                return None
            return row[i]

        def bathrooms(acct: str, bnum: int, row: list[str]) -> str:
            count = fixtures_aggregator.get_bathroom_count(acct, bnum)
            if count > 0:
                return repr(float(count))
            full = _safe_float(get(row, "full_baths")) or 0
            half = _safe_int(get(row, "half_baths")) or 0
            total = full + half * 0.5
            return repr(total) if total > 0 else _COPY_NULL

        def bedrooms(acct: str, bnum: int, row: list[str]) -> str:
            count = fixtures_aggregator.get_bedroom_count(acct, bnum)
            if count > 0:
                return str(count)
            return _int(get(row, "bedrooms"))

        def half_baths(acct: str, bnum: int, row: list[str]) -> str:
            fx = fixtures_aggregator.get_fixtures(acct, bnum)
            hb = int(fx["half_baths"])
            if hb > 0:
                return str(hb)
            return _int(get(row, "half_baths"))

        def rows() -> Iterator[str]:
            nonlocal loaded, invalid, skipped
            for row in reader:
                acct = (get(row, "account_number") or "").strip()
                if not acct:
                    skipped += 1
                    continue
                property_id = account_map.get(acct)
                if not property_id:
                    invalid += 1
                    continue

                bnum_raw = _safe_int(get(row, "building_number"))
                bnum = bnum_raw if bnum_raw is not None else 1

                fields = [
                    str(property_id),
                    _s(acct, 20),
                    str(bnum),
                    _s(get(row, "building_type"), 10),
                    _s(get(row, "building_style"), 10),
                    _s(get(row, "building_class"), 10),
                    _s(get(row, "quality_code"), 10),
                    _s(get(row, "condition_code"), 10),
                    _int(get(row, "year_built")),
                    _int(get(row, "year_remodeled")),
                    _int(get(row, "effective_year")),
                    _dec(get(row, "heat_area")),
                    _dec(get(row, "base_area")),
                    _dec(get(row, "gross_area")),
                    _dec(get(row, "stories")),
                    _s(get(row, "foundation_type"), 10),
                    _s(get(row, "exterior_wall"), 10),
                    _s(get(row, "roof_cover"), 10),
                    _s(get(row, "roof_type"), 10),
                    bedrooms(acct, bnum, row),
                    bathrooms(acct, bnum, row),
                    half_baths(acct, bnum, row),
                    _int(get(row, "fireplaces")),
                    "t",  # is_active
                    _copy_field(import_date),
                    _copy_field(batch_id),
                    _copy_field(import_date),  # created_at
                    _copy_field(import_date),  # updated_at
                ]
                loaded += 1
                yield "\t".join(fields) + "\n"

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
                _GeneratorIO(rows()),
            )
    finally:
        fh.close()

    logger.info(
        "COPY-loaded %s building records (invalid %s, skipped %s)", loaded, invalid, skipped
    )
    return {"loaded": loaded, "invalid": invalid, "skipped": skipped}


def _safe_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(float(value.strip()))
    except (ValueError, TypeError):
        return None


def _safe_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.strip())
    except (ValueError, TypeError):
        return None


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
