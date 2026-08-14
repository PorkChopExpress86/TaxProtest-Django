"""Single source of truth for parsing HCAD source files into loadable rows.

Both the COPY path (``fast_loader.copy_load``) and the ORM path
(``model_loader.ModelLoader.bulk_load``) consume the same
``RowResult`` generators from this module.  This eliminates the
~325 lines of duplicated column mappings, type coercers, and
business logic that previously lived in both ``fast_loader.py`` and
``model_loader.py``.

Each ``iter_*_rows`` function:
1. Opens the source file as a positional ``csv.reader`` (QUOTE_NONE).
2. Resolves source columns to field indices from the header row.
3. Applies business logic (residential filter, address building,
   account validation, fixtures-based bed/bath resolution).
4. Yields ``RowResult`` instances whose ``values`` list is in
   the same order as the corresponding ``FIELD_ORDER`` tuple.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from counties.harris.residential import is_residential_state_class, normalize_state_class

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RowResult:
    """A single processed row, ready for COPY or ORM consumption.

    Attributes:
        values: Field values in ``FIELD_ORDER`` order (strings for COPY,
            already coerced; the ORM path converts as needed).
        field_names: The ordered field names matching ``values``.
        skip: Row was filtered (non-residential, blank account) — count and ignore.
        invalid: Row failed validation (account not in map) — count and ignore.
    """

    values: list[str]
    field_names: list[str]
    skip: bool = False
    invalid: bool = False


# ---------------------------------------------------------------------------
# Column-source mappings (consolidated from fast_loader + transform.py)
# ---------------------------------------------------------------------------

REAL_ACCT_SOURCES: dict[str, list[str]] = {
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

BUILDING_RES_SOURCES: dict[str, list[str]] = {
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

EXTRA_FEATURES_SOURCES: dict[str, list[str]] = {
    "account_number": ["acct"],
    "feature_number": ["bld_num"],
    "feature_code": ["cd"],
    "feature_description": ["l_dscr", "dscr"],
    "quantity": ["count", "units"],
    "length": ["length"],
    "width": ["width"],
    "quality_code": ["grade"],
    "condition_code": ["cond_cd"],
    "year_built": ["act_yr"],
    "value": ["uts", "asd_val"],
    "area": ["area"],
}


# Field order for each file type — matches the model's column order
# (excluding id, timestamps, and auto-managed fields which the loader
# or the database supplies).

PROPERTY_FIELD_ORDER = [
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
]

BUILDING_FIELD_ORDER = [
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
]

EXTRA_FEATURE_FIELD_ORDER = [
    "property_id",
    "account_number",
    "feature_number",
    "feature_code",
    "feature_description",
    "quantity",
    "area",
    "length",
    "width",
    "quality_code",
    "condition_code",
    "year_built",
    "value",
    "is_active",
]


# ---------------------------------------------------------------------------
# Shared type coercers (consolidated from fast_loader + model_loader)
# ---------------------------------------------------------------------------


def coerce_str(value: str | None, maxlen: int | None = None) -> str:
    """Trim and optionally cap a string field."""
    if not value:
        return ""
    s = value.strip()
    if maxlen is not None:
        s = s[:maxlen]
    return s


def coerce_int(value: str | None) -> int | None:
    """Parse an int from a string, returning None on failure."""
    if not value:
        return None
    try:
        return int(float(value.strip()))
    except (ValueError, TypeError):
        return None


def coerce_decimal(value: str | None) -> float | None:
    """Parse a numeric value, stripping $ and commas, returning None on failure."""
    if not value:
        return None
    v = value.strip().replace("$", "").replace(",", "")
    if not v:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _open_text(filepath: Path) -> tuple[Any, Any]:
    """Open a HCAD data file as a tab-delimited positional csv.reader."""
    fh = open(filepath, encoding="latin-1", errors="ignore", newline="")
    reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
    return reader, fh


def _resolve_indices(header: list[str], source_names: dict[str, list[str]]) -> dict[str, int]:
    """Map each logical field to its column index using the header row."""
    lower_to_idx = {name.lower(): i for i, name in enumerate(header) if name is not None}
    indices: dict[str, int] = {}
    for field_name, candidates in source_names.items():
        for cand in candidates:
            idx = lower_to_idx.get(cand.lower())
            if idx is not None:
                indices[field_name] = idx
                break
    return indices


def _make_getter(idx: dict[str, int]):
    """Return a positional accessor for a resolved column-index map."""

    def get(row: list[str], field_name: str) -> str | None:
        i = idx.get(field_name)
        if i is None or i >= len(row):
            return None
        return row[i]

    return get


# ---------------------------------------------------------------------------
# PropertyRecord (real_acct.txt)
# ---------------------------------------------------------------------------


def iter_property_rows(filepath: Path) -> Iterator[RowResult]:
    """Yield RowResults for residential PropertyRecord rows from real_acct.txt.

    Non-residential rows and rows with blank account numbers are yielded
    with ``skip=True`` so callers can count them.
    """
    reader, fh = _open_text(filepath)
    field_names = list(PROPERTY_FIELD_ORDER)
    try:
        header = next(reader, None)
        if header is None:
            return
        idx = _resolve_indices(header, REAL_ACCT_SOURCES)
        get = _make_getter(idx)

        for row in reader:
            acct = coerce_str(get(row, "account_number"), 20)
            if not acct:
                yield RowResult(values=[], field_names=field_names, skip=True)
                continue

            state_class = normalize_state_class(get(row, "state_class"))[:10]
            if not is_residential_state_class(state_class):
                yield RowResult(values=[], field_names=field_names, skip=True)
                continue

            street_num = coerce_str(get(row, "street_number"), 16)
            street_name_base = coerce_str(get(row, "street_name"))
            street_suffix = coerce_str(get(row, "street_suffix"))
            street_name = (
                f"{street_name_base} {street_suffix}".strip() if street_suffix else street_name_base
            )
            street_name = coerce_str(street_name, 128)
            site_addr = coerce_str(get(row, "site_addr_1"), 255)
            address = site_addr or f"{street_num} {street_name}".strip()

            values = [
                acct,
                address[:255],
                coerce_str(get(row, "city"), 100),
                coerce_str(get(row, "zipcode"), 20),
                coerce_str(get(row, "owner_name"), 255),
                coerce_decimal(get(row, "value")),
                coerce_decimal(get(row, "assessed_value")),
                coerce_decimal(get(row, "building_area")),
                coerce_decimal(get(row, "land_area")),
                state_class,
                True,  # is_residential
                False,  # is_data_ready
                street_num,
                street_name,
                "",  # source_url
                "",  # parcel_id
            ]
            yield RowResult(values=values, field_names=field_names)
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# BuildingDetail (building_res.txt)
# ---------------------------------------------------------------------------


def iter_building_rows(
    filepath: Path,
    account_map: dict[str, int],
    fixtures_aggregator: Any,
) -> Iterator[RowResult]:
    """Yield RowResults for BuildingDetail rows from building_res.txt.

    ``account_map`` maps account numbers to PropertyRecord ids (residential
    only). ``fixtures_aggregator`` supplies bedroom/bathroom counts pre-loaded
    from fixtures.txt. Rows whose account is not in ``account_map`` are
    yielded with ``invalid=True``.
    """
    reader, fh = _open_text(filepath)
    field_names = list(BUILDING_FIELD_ORDER)
    try:
        header = next(reader, None)
        if header is None:
            return
        idx = _resolve_indices(header, BUILDING_RES_SOURCES)
        get = _make_getter(idx)

        for row in reader:
            acct = coerce_str(get(row, "account_number"), 20)
            if not acct:
                yield RowResult(values=[], field_names=field_names, skip=True)
                continue

            property_id = account_map.get(acct)
            if not property_id:
                yield RowResult(values=[], field_names=field_names, invalid=True)
                continue

            bnum = coerce_int(get(row, "building_number"))
            if bnum is None:
                bnum = 1

            # Fixtures-based bed/bath resolution
            bedroom_count = fixtures_aggregator.get_bedroom_count(acct, bnum)
            if bedroom_count > 0:
                bedrooms_val = bedroom_count
            else:
                bedrooms_val = coerce_int(get(row, "bedrooms"))

            bathroom_count = fixtures_aggregator.get_bathroom_count(acct, bnum)
            if bathroom_count > 0:
                bathrooms_val = bathroom_count
            else:
                full = coerce_decimal(get(row, "full_baths")) or 0
                half = coerce_int(get(row, "half_baths")) or 0
                total = full + half * 0.5
                bathrooms_val = total if total > 0 else None

            fixtures = fixtures_aggregator.get_fixtures(acct, bnum)
            half_bath_count = int(fixtures["half_baths"])
            if half_bath_count > 0:
                half_baths_val = half_bath_count
            else:
                half_baths_val = coerce_int(get(row, "half_baths"))

            values = [
                str(property_id),
                acct,
                str(bnum),
                coerce_str(get(row, "building_type"), 10),
                coerce_str(get(row, "building_style"), 10),
                coerce_str(get(row, "building_class"), 10),
                coerce_str(get(row, "quality_code"), 10),
                coerce_str(get(row, "condition_code"), 10),
                coerce_int(get(row, "year_built")),
                coerce_int(get(row, "year_remodeled")),
                coerce_int(get(row, "effective_year")),
                coerce_decimal(get(row, "heat_area")),
                coerce_decimal(get(row, "base_area")),
                coerce_decimal(get(row, "gross_area")),
                coerce_decimal(get(row, "stories")),
                coerce_str(get(row, "foundation_type"), 10),
                coerce_str(get(row, "exterior_wall"), 10),
                coerce_str(get(row, "roof_cover"), 10),
                coerce_str(get(row, "roof_type"), 10),
                bedrooms_val,
                bathrooms_val,
                half_baths_val,
                coerce_int(get(row, "fireplaces")),
                True,  # is_active
            ]
            yield RowResult(values=values, field_names=field_names)
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# ExtraFeature (extra_features.txt / extra_features_detail*.txt)
# ---------------------------------------------------------------------------


def iter_extra_feature_rows(
    filepath: Path,
    account_map: dict[str, int],
) -> Iterator[RowResult]:
    """Yield RowResults for ExtraFeature rows from extra_features.txt.

    ``account_map`` maps account numbers to PropertyRecord ids (residential
    only). Rows whose account is not in ``account_map`` are yielded with
    ``invalid=True``.
    """
    reader, fh = _open_text(filepath)
    field_names = list(EXTRA_FEATURE_FIELD_ORDER)
    try:
        header = next(reader, None)
        if header is None:
            return
        idx = _resolve_indices(header, EXTRA_FEATURES_SOURCES)
        get = _make_getter(idx)

        for row in reader:
            acct = coerce_str(get(row, "account_number"), 20)
            if not acct:
                yield RowResult(values=[], field_names=field_names, skip=True)
                continue

            property_id = account_map.get(acct)
            if not property_id:
                yield RowResult(values=[], field_names=field_names, invalid=True)
                continue

            values = [
                str(property_id),
                acct,
                coerce_int(get(row, "feature_number")),
                coerce_str(get(row, "feature_code"), 10),
                coerce_str(get(row, "feature_description"), 255),
                coerce_decimal(get(row, "quantity")),
                coerce_decimal(get(row, "area")),
                coerce_decimal(get(row, "length")),
                coerce_decimal(get(row, "width")),
                coerce_str(get(row, "quality_code"), 10),
                coerce_str(get(row, "condition_code"), 10),
                coerce_int(get(row, "year_built")),
                coerce_decimal(get(row, "value")),
                True,  # is_active
            ]
            yield RowResult(values=values, field_names=field_names)
    finally:
        fh.close()
