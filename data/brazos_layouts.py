"""Fixed-width record layouts for the Brazos CAD (PACS) appraisal export.

Brazos Central Appraisal District publishes its certified roll in the Harris
Govern / True Automation **PACS Appraisal Transfer** format: one fixed-width
``.TXT`` file per record type, CRLF-terminated, no header row, blank-padded.

Positions below come from the published layout
(``Appraisal-Export-Layout-8.0.25-2.pdf``, version 03/24/2021) and were verified
against the real 2025 certified export.

Two things the layout document does not make obvious, both handled here:

* **Numerics are inconsistently encoded.** Some fields carry an explicit decimal
  point (``0006193.000000``), others imply a fixed number of decimals with no
  point at all (``00000005739000`` = 573.9000 acres). ``Field.scale`` records the
  implied decimals; a literal ``.`` in the data always wins.
* **Padding is inconsistent.** Most numerics are zero-padded, but some are
  blank-padded (``'        105024'``). Everything is stripped before conversion.

The property record is also *wider in practice than the spec*: the 2025 export
emits 9247-character rows against a documented 9067. Only leading fields are
mapped, and rows are never length-validated against a total, so trailing
additions in future exports are ignored rather than fatal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Values PACS uses for its single-character boolean flags.
_TRUE_TOKENS = frozenset({"T", "Y", "1"})
_FALSE_TOKENS = frozenset({"F", "N", "0"})

TEXT = "text"
INT = "int"
DECIMAL = "decimal"
BOOL = "bool"

# PostgreSQL column type used for each field kind in the staging table.
_STAGING_TYPES = {
    TEXT: "text",
    INT: "bigint",
    DECIMAL: "numeric",
    BOOL: "boolean",
}


@dataclass(frozen=True)
class Field:
    """One fixed-width column, mapped to a database column of the same name.

    ``start``/``end`` are 1-based and inclusive, matching the layout document
    verbatim so the tables below can be checked against the PDF line by line.
    """

    name: str
    start: int
    end: int
    kind: str = TEXT
    scale: int = 0
    optional: bool = False
    """Field that lies beyond the published layout. Excluded from ``min_width``
    so an export that stops short of it still loads, with the column left NULL
    rather than the whole file being rejected."""

    @property
    def slice(self) -> slice:
        return slice(self.start - 1, self.end)

    @property
    def staging_type(self) -> str:
        return _STAGING_TYPES[self.kind]


@dataclass(frozen=True)
class FileLayout:
    """A PACS export file and the model it loads into."""

    suffix: str
    """Filename suffix inside the archive. Real names are prefixed with the
    export date and dataset id, e.g. ``2025-07-23_002022_APPRAISAL_INFO.TXT``."""

    model: str
    label: str
    fields: tuple[Field, ...]
    conflict_columns: tuple[str, ...]
    """Natural key used as the ``ON CONFLICT`` target, making re-imports idempotent."""

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    @property
    def min_width(self) -> int:
        """Shortest record this layout can read without truncating a required field."""
        return max(f.end for f in self.fields if not f.optional)


def _convert(field: Field, raw: str) -> str | None:
    """Convert one raw fixed-width slice to a value for PostgreSQL's text COPY.

    Returns ``None`` for SQL NULL. Unparseable numerics become NULL rather than
    aborting the load — a single malformed row in a multi-gigabyte export should
    not cost the whole import.

    Blank text stays an empty string rather than becoming NULL, so the target
    columns can use Django's usual ``blank=True`` (NOT NULL) convention.
    """
    value = raw.strip()

    if field.kind == TEXT:
        return value

    if not value:
        return None

    if field.kind == BOOL:
        token = value.upper()
        if token in _TRUE_TOKENS:
            return "t"
        if token in _FALSE_TOKENS:
            return "f"
        return None

    if field.kind == INT:
        try:
            return str(int(value))
        except ValueError:
            return None

    # DECIMAL: an explicit decimal point always wins over the implied scale.
    if "." in value:
        try:
            return str(Decimal(value))
        except InvalidOperation:
            return None
    try:
        scaled = Decimal(value)
    except InvalidOperation:
        return None
    if field.scale:
        scaled = scaled.scaleb(-field.scale)
    return str(scaled)


# ---------------------------------------------------------------------------
# File #2 — Property (APPRAISAL_INFO.TXT)
#
# The full record carries 400+ columns, the bulk of them agent/mortgage contact
# blocks, per-exemption proration dates and explicit "Not In Use" filler. Mapped
# here is the subset that matters for valuation and protest work: identity,
# situs, legal description, the value components, and the homestead-relevant
# exemption flags.
# ---------------------------------------------------------------------------
APPRAISAL_INFO = FileLayout(
    suffix="APPRAISAL_INFO.TXT",
    model="PropertyAccount",
    label="property accounts",
    conflict_columns=("prop_id", "tax_year"),
    fields=(
        Field("prop_id", 1, 12, INT),
        Field("prop_type_cd", 13, 17),
        Field("tax_year", 18, 22, INT),
        Field("sup_num", 23, 34, INT),
        Field("geo_id", 547, 596),
        Field("py_owner_id", 597, 608, INT),
        Field("owner_name", 609, 678),
        Field("owner_addr_line1", 694, 753),
        Field("owner_addr_line2", 754, 813),
        Field("owner_addr_line3", 814, 873),
        Field("owner_addr_city", 874, 923),
        Field("owner_addr_state", 924, 973),
        Field("owner_addr_zip", 979, 983),
        # The street *number* sits ~3.3 KB away from the rest of the address,
        # at 4460, rather than beside situs_street. Missing it leaves every
        # situs looking like an unnumbered street name.
        Field("situs_num", 4460, 4474),
        Field("situs_unit", 4475, 4479),
        Field("situs_street_prefix", 1040, 1049),
        Field("situs_street", 1050, 1099),
        Field("situs_street_suffix", 1100, 1109),
        Field("situs_city", 1110, 1139),
        Field("situs_zip", 1140, 1149),
        Field("legal_desc", 1150, 1404),
        Field("legal_desc2", 1405, 1659),
        Field("legal_acreage", 1660, 1675, DECIMAL, scale=4),
        Field("abs_subdv_cd", 1676, 1685),
        Field("hood_cd", 1686, 1695),
        Field("block", 1696, 1745),
        Field("tract_or_lot", 1746, 1795),
        Field("land_hstd_val", 1796, 1810, DECIMAL),
        Field("land_non_hstd_val", 1811, 1825, DECIMAL),
        Field("imprv_hstd_val", 1826, 1840, DECIMAL),
        Field("imprv_non_hstd_val", 1841, 1855, DECIMAL),
        Field("ag_use_val", 1856, 1870, DECIMAL),
        Field("ag_market", 1871, 1885, DECIMAL),
        Field("timber_use", 1886, 1900, DECIMAL),
        Field("timber_market", 1901, 1915, DECIMAL),
        # Despite the names, these two are computed *before* the agricultural
        # productivity deduction — appraised_val is what the district publishes
        # as "Market Value" and equals the sum of the components above. The
        # post-deduction figures live in their own fields 6.7 KB later.
        Field("appraised_val", 1916, 1930, DECIMAL),
        Field("ten_percent_cap", 1931, 1945, DECIMAL),
        Field("assessed_val", 1946, 1960, DECIMAL),
        # These match the district's published "Appraised Value" and "Assessed
        # Value" for every property, ag or not. Prefer them over the two above.
        Field("appraised_val_prod_loss", 8603, 8617, DECIMAL),
        Field("assessed_val_prod_loss", 8618, 8632, DECIMAL),
        # Beyond the published 9067-character layout. Brazos emits 9247-character
        # records whose first extra field is the SB2 (2023) circuit-breaker
        # limitation: verified as appraised_val - circuit_breaker_val ==
        # assessed_val on all 7,060 affected 2025 rows. Marked optional so an
        # export that predates the extension still loads.
        Field("circuit_breaker_val", 9068, 9082, DECIMAL, optional=True),
        Field("arb_protest_flag", 1981, 1981, BOOL),
        Field("deed_dt", 2034, 2058),
        Field("hs_exempt", 2609, 2609, BOOL),
        Field("ov65_exempt", 2610, 2610, BOOL),
        Field("dp_exempt", 2662, 2662, BOOL),
        Field("imprv_state_cd", 2732, 2741),
        Field("land_state_cd", 2742, 2751),
        Field("personal_state_cd", 2752, 2761),
        Field("land_acres", 2772, 2791, DECIMAL, scale=4),
        # Comma-separated taxing units for this account, e.g. "C2, CAD, G1, S2".
        # Populated on 100% of 2025 rows and the only per-property jurisdiction
        # data in this file — APPRAISAL_ENTITY.TXT is just a code lookup.
        Field("entities", 5202, 5341),
        Field("dataset_id", 5343, 5357, INT),
    ),
)

# ---------------------------------------------------------------------------
# File #10 — Land (APPRAISAL_LAND_DETAIL.TXT), 199-character records
# ---------------------------------------------------------------------------
APPRAISAL_LAND_DETAIL = FileLayout(
    suffix="APPRAISAL_LAND_DETAIL.TXT",
    model="PropertyLand",
    label="land segments",
    conflict_columns=("prop_id", "tax_year", "land_seg_id"),
    fields=(
        Field("prop_id", 1, 12, INT),
        Field("tax_year", 13, 16, INT),
        Field("land_seg_id", 17, 28, INT),
        Field("land_type_cd", 29, 38),
        Field("land_type_desc", 39, 63),
        Field("state_cd", 64, 68),
        Field("land_seg_homesite", 69, 69, BOOL),
        Field("size_acres", 70, 83, DECIMAL, scale=4),
        Field("size_square_feet", 84, 97, DECIMAL),
        Field("effective_front", 98, 111, DECIMAL),
        Field("effective_depth", 112, 125, DECIMAL),
        Field("mkt_ls_method", 126, 130),
        Field("mkt_ls_class", 131, 140),
        Field("land_seg_mkt_val", 141, 154, DECIMAL),
        Field("ag_apply", 155, 155, BOOL),
        Field("ag_ls_method", 156, 160),
        Field("ag_ls_class", 161, 170),
        Field("ag_value", 171, 184, DECIMAL),
        Field("land_homesite_pct", 185, 199, DECIMAL),
    ),
)

# ---------------------------------------------------------------------------
# File #7 — Improvements (APPRAISAL_IMPROVEMENT_INFO.TXT), 114-character records
# ---------------------------------------------------------------------------
APPRAISAL_IMPROVEMENT_INFO = FileLayout(
    suffix="APPRAISAL_IMPROVEMENT_INFO.TXT",
    model="PropertyImprovement",
    label="improvements",
    conflict_columns=("prop_id", "tax_year", "imp_id"),
    fields=(
        Field("prop_id", 1, 12, INT),
        Field("tax_year", 13, 16, INT),
        Field("imp_id", 17, 28, INT),
        Field("imprv_type_cd", 29, 38),
        Field("imprv_type_desc", 39, 63),
        Field("imprv_state_cd", 64, 68),
        Field("imprv_homesite", 69, 69, BOOL),
        Field("imprv_val", 70, 83, DECIMAL),
        Field("imprv_homesite_pct", 84, 98, DECIMAL),
        Field("omitted", 99, 99, BOOL),
        Field("omitted_imprv_val", 100, 114, DECIMAL),
    ),
)

# ---------------------------------------------------------------------------
# File #8 — Improvement Detail (APPRAISAL_IMPROVEMENT_DETAIL.TXT), 622 chars
#
# ``sketch_cmds`` is the PACS drawing program for the footprint (e.g.
# "R30,U60,..."). Kept because it is the only machine-readable record of how the
# district derived the improvement area — directly relevant when disputing sqft.
# ---------------------------------------------------------------------------
APPRAISAL_IMPROVEMENT_DETAIL = FileLayout(
    suffix="APPRAISAL_IMPROVEMENT_DETAIL.TXT",
    model="PropertyImprovementDetail",
    label="improvement details",
    conflict_columns=("prop_id", "tax_year", "imp_id", "imprv_det_id"),
    fields=(
        Field("prop_id", 1, 12, INT),
        Field("tax_year", 13, 16, INT),
        Field("imp_id", 17, 28, INT),
        Field("imprv_det_id", 29, 40, INT),
        Field("imprv_det_type_cd", 41, 50),
        Field("imprv_det_type_desc", 51, 75),
        Field("imprv_det_class_cd", 76, 85),
        Field("yr_built", 86, 89, INT),
        Field("depreciation_yr", 90, 93, INT),
        Field("imprv_det_area", 94, 108, DECIMAL),
        Field("imprv_det_val", 109, 122, DECIMAL),
        Field("sketch_cmds", 123, 622),
    ),
)

# ---------------------------------------------------------------------------
# File #14 — Entity (APPRAISAL_ENTITY.TXT), 17-character records
#
# The taxing-jurisdiction code list (~43 rows: school districts, city, county,
# college, ESDs). Per-property jurisdiction assignments and their exemption
# amounts live in File #3, APPRAISAL_ENTITY_INFO.TXT, which is not loaded here.
# ---------------------------------------------------------------------------
APPRAISAL_ENTITY = FileLayout(
    suffix="APPRAISAL_ENTITY.TXT",
    model="PropertyEntity",
    label="taxing entities",
    conflict_columns=("entity_id",),
    fields=(
        Field("entity_id", 1, 12, INT),
        Field("entity_cd", 13, 17),
    ),
)


# ---------------------------------------------------------------------------
# File #1 — Header (APPRAISAL_HEADER.TXT), one record
#
# Records the exact vintage of an export. Without it "the 2025 data" is
# ambiguous: the district re-exports through the year as supplements are
# certified, and two files both labelled 2025 can hold different values.
# Not a table of its own — it annotates the import run.
# ---------------------------------------------------------------------------
HEADER_SUFFIX = "APPRAISAL_HEADER.TXT"
HEADER_FIELDS: tuple[Field, ...] = (
    Field("run_date", 1, 16),
    Field("file_description", 17, 56),
    Field("appraisal_year", 57, 60, INT),
    Field("supplement_num", 61, 64, INT),
    Field("entity_cd", 65, 74),
    Field("office_name", 115, 144),
    Field("operator", 145, 164),
    Field("pacs_version", 165, 174),
)


def parse_header(line: str) -> dict[str, str | None]:
    """Parse the single-record header file into a provenance dict."""
    return {field.name: _convert(field, line[field.slice]) for field in HEADER_FIELDS}


# Load order matters: accounts first so child records reference an account that
# already exists, entities last because nothing depends on them.
LAYOUTS: tuple[FileLayout, ...] = (
    APPRAISAL_INFO,
    APPRAISAL_LAND_DETAIL,
    APPRAISAL_IMPROVEMENT_INFO,
    APPRAISAL_IMPROVEMENT_DETAIL,
    APPRAISAL_ENTITY,
)

LAYOUTS_BY_MODEL = {layout.model: layout for layout in LAYOUTS}


def parse_record(layout: FileLayout, line: str) -> list[str | None]:
    """Slice one fixed-width record into converted column values."""
    return [_convert(field, line[field.slice]) for field in layout.fields]
