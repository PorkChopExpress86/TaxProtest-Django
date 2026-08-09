"""BCAD PACS-format fixed-width parsers.

The BCAD certified export (``APPRAISAL_*.TXT``) is fixed-width, CRLF-terminated,
with no delimiter and no header row. Offsets below were verified against a real
2025 certified export (see ``counties/brazos/var/extracted/<year>/`` after running
``load_brazos_cad``) by scanning entire files for uniform line length, per-column
value-frequency, and cross-file value matches — not guessed from the file
headers a normal CSV would have. See ``counties/brazos/tests/test_parsers.py`` for
fixtures that pin these offsets down as regression tests.

Money fields carry 2 implied decimal places (``_money``); the acreage field
carries 4 implied decimal places (``_qty4``); ``detail_value``/``detail_quantity``
in APPRAISAL_IMPROVEMENT_DETAIL.TXT already contain a literal decimal point
(``_decimal_literal``, no scaling).

Caveat (resolved, see wayfinder ticket #4): the address block in
``APPRAISAL_INFO.TXT`` -- ``mailing_address`` etc. below -- is confirmed to be
the *owner's mailing* address, not the property's physical (situs) location:
it's duplicated elsewhere in the same 9,247-char record alongside
owner-identifying fields, at least one sampled row has an internally
inconsistent city/state/zip combination consistent with two different
real-world addresses being read through the same byte range, and it matches
the GIS parcel shapefile's own separate mailing-address fields exactly for a
cross-checked test property. The true situs address comes from that
shapefile instead (``load_brazos_gis``, not this module).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    """One fixed-width field: ``line[start:end]`` cast via ``cast``."""

    name: str
    start: int
    end: int
    cast: Callable[[str], Any]


_WHITESPACE_RE = re.compile(r"\s+")


def _text(raw: str) -> str:
    """Strip and collapse internal whitespace runs to a single space.

    Confirmed real: APPRAISAL_INFO.TXT's owner_name field can contain a
    literal double space mid-string (e.g. "MILLER NORMAN B  & LESLIE B",
    verified against raw bytes) -- an artifact of how BCAD concatenates
    name components, not something worth preserving. BCAD's own live
    property search collapses it for display; matching that avoids a
    false-positive mismatch when cross-checking ingested data against it
    (see validate_brazos_against_source)."""
    return _WHITESPACE_RE.sub(" ", raw.strip())


def _money(raw: str) -> Decimal | None:
    """2 implied decimal places, e.g. ``'00000002833677'`` -> ``Decimal('28336.77')``."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw) / 100
    except InvalidOperation:
        return None


def _qty4(raw: str) -> Decimal | None:
    """4 implied decimal places (acreage only), e.g. ``'105024'`` -> ``Decimal('10.5024')``."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw) / 10000
    except InvalidOperation:
        return None


def _decimal_literal(raw: str) -> Decimal | None:
    """Field already contains a literal decimal point, e.g. ``'00001800.000000'``."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _money_flat(raw: str) -> Decimal | None:
    """No implied decimal places (APPRAISAL_ENTITY_INFO.TXT only), e.g.
    ``'000000000075000'`` -> ``Decimal('75000')``. Confirmed by reconciling
    non-round-number rows against assessed_val - taxable_val (see
    docs/research/brazos-exemptions.md) -- do NOT use ``_money`` here, these
    fields are NOT 2-implied-decimal like the rest of this export family."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _int_or_none(raw: str) -> int | None:
    """Blank or all-zero (e.g. year_built '0000' meaning N/A) -> None."""
    raw = raw.strip()
    if not raw or raw.strip("0") == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _int_or_zero(raw: str) -> int:
    """For NOT NULL integer fields (land_seq, detail_seq) — never returns None."""
    raw = raw.strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def parse_fixed_width(line: str, layout: Sequence[FieldSpec]) -> dict[str, Any]:
    return {spec.name: spec.cast(line[spec.start : spec.end]) for spec in layout}


# ---------------------------------------------------------------- APPRAISAL_LAND_DETAIL.TXT
# 199 chars/line. -> brazos_cad.models.PropertyLand
LAND_DETAIL_LAYOUT: tuple[FieldSpec, ...] = (
    FieldSpec("prop_id", 0, 12, _text),
    FieldSpec("tax_year", 12, 16, _int_or_none),
    FieldSpec("land_seq", 16, 28, _int_or_zero),
    FieldSpec("land_use_code", 28, 32, _text),
    FieldSpec("land_use_description", 38, 63, _text),
    FieldSpec("land_value", 140, 154, _money),
    FieldSpec("acreage", 178, 184, _qty4),
)


def parse_land_detail_line(line: str) -> dict[str, Any]:
    return parse_fixed_width(line, LAND_DETAIL_LAYOUT)


# ------------------------------------------------------------ APPRAISAL_IMPROVEMENT_INFO.TXT
# 114 chars/line. -> brazos_cad.models.PropertyImprovement
# improvement_value and square_feet are NOT populated from this file: the
# improvement_value tail field is confirmed zero on all 94,291 real rows, and
# the two decimal fields near offset 69-98 have unclear/unreliable semantics.
IMPROVEMENT_INFO_LAYOUT: tuple[FieldSpec, ...] = (
    FieldSpec("prop_id", 0, 12, _text),
    FieldSpec("tax_year", 12, 16, _int_or_none),
    FieldSpec("imp_id", 16, 28, _text),
    FieldSpec("improvement_type", 28, 31, _text),
    FieldSpec("improvement_description", 38, 49, _text),
)


def parse_improvement_info_line(line: str) -> dict[str, Any]:
    return parse_fixed_width(line, IMPROVEMENT_INFO_LAYOUT)


# ---------------------------------------------------------- APPRAISAL_IMPROVEMENT_DETAIL.TXT
# 622 chars/line. -> brazos_cad.models.PropertyImprovementDetail
# detail_unit (122:622) is a free-text tail, NOT a short unit code — real
# content runs up to ~500 chars (construction/perimeter codes like
# "R30,U60,L30,D60"), hence the model field is a TextField, not CharField(32).
IMPROVEMENT_DETAIL_LAYOUT: tuple[FieldSpec, ...] = (
    FieldSpec("prop_id", 0, 12, _text),
    FieldSpec("tax_year", 12, 16, _int_or_none),
    FieldSpec("imp_id", 16, 28, _text),
    FieldSpec("detail_seq", 28, 40, _int_or_zero),
    FieldSpec("detail_description", 50, 75, _text),
    FieldSpec("year_built", 85, 89, _int_or_none),
    FieldSpec("detail_quantity", 93, 108, _decimal_literal),
    FieldSpec("detail_value", 108, 122, _decimal_literal),
    FieldSpec("detail_unit", 122, 622, _text),
)


def parse_improvement_detail_line(line: str) -> dict[str, Any]:
    return parse_fixed_width(line, IMPROVEMENT_DETAIL_LAYOUT)


# -------------------------------------------------------------- APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT
# 87 chars/line. One row per (improvement detail, attribute) -- bedroom/
# bathroom/room counts and construction-material codes, plus feature rows
# (fireplace, patio, carport, pool, etc.), encoded as attribute-type/value
# pairs rather than columns. See load_brazos_cad.py for how these get
# classified/routed into PropertyBuildingCharacteristic (wide, one row per
# improvement) vs PropertyExtraFeature (long, one row per feature).
# `detail_seq` (28:40) verified to match PropertyImprovementDetail.detail_seq
# for the same imp_id -- confirms each attribute row is tied to a specific
# detail row, not just the improvement as a whole. The second unlabeled
# field (40:52) was checked and is NOT a reliable unique row id (325,289
# distinct values across 394,291 rows) -- not decoded further, not needed.
# `attribute_value` (77:87) is only 10 chars -- confirmed BCAD genuinely
# truncates free-text "Other Feature" values there (real sampled values:
# "STORAGE BU", "COVERED PA", "WOOD DECK "), not a parsing artifact.
IMPROVEMENT_DETAIL_ATTR_LAYOUT: tuple[FieldSpec, ...] = (
    FieldSpec("prop_id", 0, 12, _text),
    FieldSpec("tax_year", 12, 16, _int_or_none),
    FieldSpec("imp_id", 16, 28, _text),
    FieldSpec("detail_seq", 28, 40, _int_or_zero),
    FieldSpec("attribute_type", 52, 77, _text),
    FieldSpec("attribute_value", 77, 87, _text),
)


def parse_improvement_detail_attr_line(line: str) -> dict[str, Any]:
    return parse_fixed_width(line, IMPROVEMENT_DETAIL_ATTR_LAYOUT)


# ------------------------------------------------------------------------ APPRAISAL_ENTITY.TXT
# 17 chars/line. This file is a bare entity_id -> entity_code lookup table,
# NOT an entity master — entity_name/entity_type/adopted_rate/effective_date
# (what brazos_cad.models.PropertyEntity actually wants) are not present here
# at all. Kept for completeness/testing but NOT wired into INGEST_SPECS.
ENTITY_LAYOUT: tuple[FieldSpec, ...] = (
    FieldSpec("entity_id", 0, 12, _text),
    FieldSpec("entity_code", 12, 17, _text),
)


def parse_entity_line(line: str) -> dict[str, Any]:
    return parse_fixed_width(line, ENTITY_LAYOUT)


# ------------------------------------------------------------------- APPRAISAL_ENTITY_INFO.TXT
# 2750 chars/line. One row per (property, taxing entity, year) -- the real
# analog of counties.harris.models.PropertyJurisdictionExemption (wide: one column per
# exemption type, needs unpivoting into that model's long per-code-row shape).
# Offsets verified against real 2025 export bytes; see
# docs/research/brazos-exemptions.md on branch research/brazos-exemptions for
# the full methodology (wayfinder tickets #10, #11).
#
# Only hs_amt/ov65_amt/dp_amt are parsed here -- the only three amount fields
# independently confirmed correct via cross-checks against freeze_exempt_type_cd
# and assessed_val-taxable_val reconciliation. dv_amt is deliberately NOT
# parsed: research found it does not reliably represent the total
# disabled-veteran exemption (one sampled property with a DV exemption showed
# dv_amt=0 despite taxable_val=$0), so including it risks silently
# understating a real exemption rather than just omitting an unconfirmed one.
# The ab/en/fr/ht/pro/pc/so/ex366/ch exemption types (APPRAISAL_INFO.TXT,
# byte 2723-2731) are unverified entirely and not sourced from any file.
ENTITY_INFO_LAYOUT: tuple[FieldSpec, ...] = (
    FieldSpec("prop_id", 0, 12, _text),
    FieldSpec("tax_year", 12, 17, _int_or_none),
    FieldSpec("entity_id", 41, 53, _text),
    FieldSpec("tax_unit_code", 53, 63, _text),
    FieldSpec("tax_unit_name", 63, 113, _text),
    FieldSpec("assessed_value", 148, 163, _money_flat),
    FieldSpec("taxable_value", 163, 178, _money_flat),
    FieldSpec("hs_amt", 298, 313, _money_flat),
    FieldSpec("ov65_amt", 313, 328, _money_flat),
    FieldSpec("dp_amt", 328, 343, _money_flat),
)


def parse_entity_info_line(line: str) -> dict[str, Any]:
    return parse_fixed_width(line, ENTITY_INFO_LAYOUT)


# --------------------------------------------------------------------------- APPRAISAL_INFO.TXT
# 9247 chars/line (large composite record; only the fields below are decoded).
# -> brazos_cad.models.PropertyAccount (mailing_* fields only)
# total_value/land_value/improvement_value/assessed_value/state_class/
# latitude/longitude/living_area/year_built/class_code/situs_* are NOT
# located in this file -- they come from the GIS parcel shapefile instead
# (see load_brazos_gis). The address block below is confirmed to be the
# owner's MAILING address, not situs -- see module docstring.
INFO_LAYOUT: tuple[FieldSpec, ...] = (
    FieldSpec("prop_id", 0, 12, _text),
    FieldSpec("tax_year", 17, 22, _int_or_none),
    FieldSpec("owner_name", 608, 678, _text),
    FieldSpec("mailing_address", 753, 873, _text),
    FieldSpec("mailing_city", 873, 923, _text),
    FieldSpec("mailing_state", 923, 925, _text),
    FieldSpec("mailing_zip5", 978, 983, _text),
    FieldSpec("mailing_zip4", 983, 987, _text),
)


def parse_info_line(line: str) -> dict[str, Any]:
    data = parse_fixed_width(line, INFO_LAYOUT)
    zip5 = data.pop("mailing_zip5", "") or ""
    zip4 = data.pop("mailing_zip4", "") or ""
    data["mailing_zip"] = f"{zip5}-{zip4}" if zip5 and zip4 else zip5
    return data
