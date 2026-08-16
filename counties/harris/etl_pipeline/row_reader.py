"""Single source of truth for parsing HCAD source files into typed rows.

Both the COPY path (``fast_loader.copy_load_*``) and the ORM path
(``model_loader.ModelLoader.load_*``) consume the same ``RowResult``
generators from this module.  This concentrates the column mappings, type
coercers, and business logic (residential filter, address assembly,
fixtures-based bed/bath resolution) that previously lived in three places.

Each ``iter_*_rows`` function:
1. Opens the source file as a positional ``csv.reader`` (QUOTE_NONE).
2. Resolves source columns to field indices from the header row.
3. Applies business logic and yields ``RowResult`` instances whose ``row``
   is a typed dataclass (``PropertyRow`` / ``BuildingRow`` /
   ``ExtraFeatureRow``), or ``None`` when the row was filtered.

Rows are filtered into two buckets, counted separately by the loaders:

- ``skip``    — non-residential or blank account; not an error.
- ``invalid`` — account not in the residential account map; not an error.

The loaders serialize the typed rows: ``fast_loader`` to COPY text,
``model_loader`` to Django model instances.
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

# HCAD free-text fields (inspector notes, addresses) contain raw, unescaped
# quote characters.  With default quoting, csv treats an unbalanced `"` as
# opening a quoted field that swallows subsequent lines until a closing quote
# turns up, silently corrupting row boundaries and dropping real records.
# These files have no quoting convention, so quotes must be read literally.
csv.field_size_limit(10485760)  # 10MB limit


# ---------------------------------------------------------------------------
# Typed row dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PropertyRow:
    """A residential PropertyRecord row, ready for COPY or ORM consumption."""

    account_number: str
    address: str
    city: str
    zipcode: str
    owner_name: str
    value: float | None
    assessed_value: float | None
    building_area: float | None
    land_area: float | None
    state_class: str
    is_residential: bool
    is_data_ready: bool
    street_number: str
    street_name: str


@dataclass
class BuildingRow:
    """A residential BuildingDetail row, ready for COPY or ORM consumption."""

    property_id: int
    account_number: str
    building_number: int
    building_type: str
    building_style: str
    building_class: str
    quality_code: str
    condition_code: str
    year_built: int | None
    year_remodeled: int | None
    effective_year: int | None
    heat_area: float | None
    base_area: float | None
    gross_area: float | None
    stories: float | None
    foundation_type: str
    exterior_wall: str
    roof_cover: str
    roof_type: str
    bedrooms: int | None
    bathrooms: float | None
    half_baths: int | None
    fireplaces: int | None
    is_active: bool


@dataclass
class ExtraFeatureRow:
    """A residential ExtraFeature row, ready for COPY or ORM consumption."""

    property_id: int
    account_number: str
    feature_number: int | None
    feature_code: str
    feature_description: str
    quantity: float | None
    length: float | None
    width: float | None
    quality_code: str
    condition_code: str
    year_built: int | None
    value: float | None
    is_active: bool


@dataclass
class RowResult:
    """A single processed row, ready for COPY or ORM consumption.

    ``row`` is the typed payload, or ``None`` when the row was filtered.
    ``skip`` marks non-residential / blank-account rows; ``invalid`` marks
    rows whose account is not in the residential account map.  Loaders count
    the flags and serialize ``row``.
    """

    row: PropertyRow | BuildingRow | ExtraFeatureRow | None
    skip: bool = False
    invalid: bool = False


# ---------------------------------------------------------------------------
# Column-source mappings
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
}


# ---------------------------------------------------------------------------
# Shared type coercers
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
    try:
        header = next(reader, None)
        if header is None:
            return
        idx = _resolve_indices(header, REAL_ACCT_SOURCES)
        get = _make_getter(idx)

        for row in reader:
            acct = coerce_str(get(row, "account_number"), 20)
            if not acct:
                yield RowResult(row=None, skip=True)
                continue

            state_class = normalize_state_class(get(row, "state_class"))[:10]
            if not is_residential_state_class(state_class):
                yield RowResult(row=None, skip=True)
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

            yield RowResult(
                row=PropertyRow(
                    account_number=acct,
                    address=address[:255],
                    city=coerce_str(get(row, "city"), 100),
                    zipcode=coerce_str(get(row, "zipcode"), 20),
                    owner_name=coerce_str(get(row, "owner_name"), 255),
                    value=coerce_decimal(get(row, "value")),
                    assessed_value=coerce_decimal(get(row, "assessed_value")),
                    building_area=coerce_decimal(get(row, "building_area")),
                    land_area=coerce_decimal(get(row, "land_area")),
                    state_class=state_class,
                    is_residential=True,
                    is_data_ready=False,
                    street_number=street_num,
                    street_name=street_name,
                )
            )
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
    try:
        header = next(reader, None)
        if header is None:
            return
        idx = _resolve_indices(header, BUILDING_RES_SOURCES)
        get = _make_getter(idx)

        for row in reader:
            acct = coerce_str(get(row, "account_number"), 20)
            if not acct:
                yield RowResult(row=None, skip=True)
                continue

            property_id = account_map.get(acct)
            if not property_id:
                yield RowResult(row=None, invalid=True)
                continue

            bnum = coerce_int(get(row, "building_number"))
            if bnum is None:
                bnum = 1

            # Fixtures-based bed/bath resolution: fixtures.txt wins, fall back
            # to the building_res.txt columns when fixtures are absent.
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

            yield RowResult(
                row=BuildingRow(
                    property_id=property_id,
                    account_number=acct,
                    building_number=bnum,
                    building_type=coerce_str(get(row, "building_type"), 10),
                    building_style=coerce_str(get(row, "building_style"), 10),
                    building_class=coerce_str(get(row, "building_class"), 10),
                    quality_code=coerce_str(get(row, "quality_code"), 10),
                    condition_code=coerce_str(get(row, "condition_code"), 10),
                    year_built=coerce_int(get(row, "year_built")),
                    year_remodeled=coerce_int(get(row, "year_remodeled")),
                    effective_year=coerce_int(get(row, "effective_year")),
                    heat_area=coerce_decimal(get(row, "heat_area")),
                    base_area=coerce_decimal(get(row, "base_area")),
                    gross_area=coerce_decimal(get(row, "gross_area")),
                    stories=coerce_decimal(get(row, "stories")),
                    foundation_type=coerce_str(get(row, "foundation_type"), 10),
                    exterior_wall=coerce_str(get(row, "exterior_wall"), 10),
                    roof_cover=coerce_str(get(row, "roof_cover"), 10),
                    roof_type=coerce_str(get(row, "roof_type"), 10),
                    bedrooms=bedrooms_val,
                    bathrooms=bathrooms_val,
                    half_baths=half_baths_val,
                    fireplaces=coerce_int(get(row, "fireplaces")),
                    is_active=True,
                )
            )
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# ExtraFeature (extra_features.txt / extra_features_detail*.txt)
# ---------------------------------------------------------------------------


def iter_extra_feature_rows(
    filepath: Path,
    account_map: dict[str, int],
) -> Iterator[RowResult]:
    """Yield RowResults for ExtraFeature rows from extra_features*.txt.

    ``account_map`` maps account numbers to PropertyRecord ids (residential
    only). Rows whose account is not in ``account_map`` are yielded with
    ``invalid=True``.
    """
    reader, fh = _open_text(filepath)
    try:
        header = next(reader, None)
        if header is None:
            return
        idx = _resolve_indices(header, EXTRA_FEATURES_SOURCES)
        get = _make_getter(idx)

        for row in reader:
            acct = coerce_str(get(row, "account_number"), 20)
            if not acct:
                yield RowResult(row=None, skip=True)
                continue

            property_id = account_map.get(acct)
            if not property_id:
                yield RowResult(row=None, invalid=True)
                continue

            yield RowResult(
                row=ExtraFeatureRow(
                    property_id=property_id,
                    account_number=acct,
                    feature_number=coerce_int(get(row, "feature_number")),
                    feature_code=coerce_str(get(row, "feature_code"), 10),
                    feature_description=coerce_str(get(row, "feature_description"), 255),
                    quantity=coerce_decimal(get(row, "quantity")),
                    length=coerce_decimal(get(row, "length")),
                    width=coerce_decimal(get(row, "width")),
                    quality_code=coerce_str(get(row, "quality_code"), 10),
                    condition_code=coerce_str(get(row, "condition_code"), 10),
                    year_built=coerce_int(get(row, "year_built")),
                    value=coerce_decimal(get(row, "value")),
                    is_active=True,
                )
            )
    finally:
        fh.close()
