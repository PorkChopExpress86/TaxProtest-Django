"""The county-neutral vocabulary the shared views and templates speak.

Two record types (:class:`Subject`, :class:`Comp`) plus a page description
(:class:`CountyProfile`) and the interface a county implements
(:class:`CountyAdapter`).

The records are deliberately flat and display-oriented: they hold what the
report shows, already resolved, so templates never reach back into a county's
ORM. Fields a county has no equivalent for stay ``None``; the templates render
an em dash and the columns a county does not declare are simply never asked for.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# --------------------------------------------------------------------------- display specs

#: HCAD-style single-letter quality grades, shared because Harris and any future
#: county using the same CAMA vocabulary should render them identically.
QUALITY_LABELS: Mapping[str, str] = {
    "X": "Superior",
    "A": "Excellent",
    "B": "Good",
    "C": "Average",
    "D": "Low",
    "E": "Very Low",
    "F": "Poor",
}


@dataclass(frozen=True)
class SearchField:
    """One input on a county's property-search form."""

    name: str
    label: str
    placeholder: str = ""
    maxlength: int | None = None
    pattern: str | None = None


@dataclass(frozen=True)
class Column:
    """One column in a results or comparables table.

    ``format`` selects how the cell renders; see
    ``templates/counties/partials/_cell.html`` for the supported values.
    """

    label: str
    key: str
    align: str = "left"
    format: str = "text"
    sort_key: str | None = None
    title: str | None = None
    truncate: bool = False

    @property
    def sortable(self) -> bool:
        return self.sort_key is not None


@dataclass(frozen=True)
class DetailRow:
    """A single labelled value on the subject-property card."""

    label: str
    value: Any
    format: str = "text"


# --------------------------------------------------------------------------- records


@dataclass
class Subject:
    """The property being analysed, normalised across counties."""

    key: str
    address_line: str
    locality_line: str = ""
    owner_name: str | None = None
    zip_code: str | None = None
    assessed_value: Decimal | float | None = None
    living_area: float | None = None
    land_area: float | None = None
    bedrooms: int | None = None
    bathrooms: float | None = None
    year_built: int | None = None
    quality_code: str | None = None
    features: str = ""
    has_location: bool = True
    tax_year: int | None = None
    #: Extra labelled values for the subject card, in display order.
    detail_rows: list[DetailRow] = field(default_factory=list)

    @property
    def value_per_sqft(self) -> float | None:
        if not self.assessed_value or not self.living_area or self.living_area <= 0:
            return None
        try:
            return float(self.assessed_value) / float(self.living_area)
        except (TypeError, ValueError, ArithmeticError):
            return None


@dataclass
class Comp:
    """One comparable property, normalised across counties."""

    key: str
    address: str
    similarity_score: float
    match_label: str
    zip_code: str | None = None
    owner_name: str | None = None
    assessed_value: Decimal | float | None = None
    living_area: float | None = None
    land_area: float | None = None
    bedrooms: int | None = None
    bathrooms: float | None = None
    year_built: int | None = None
    quality_code: str | None = None
    condition_code: str | None = None
    features: str = ""
    distance: float | None = None
    score_breakdown: list[dict[str, Any]] = field(default_factory=list)

    @property
    def value_per_sqft(self) -> float | None:
        if not self.assessed_value or not self.living_area or self.living_area <= 0:
            return None
        try:
            return float(self.assessed_value) / float(self.living_area)
        except (TypeError, ValueError, ArithmeticError):
            return None

    def delta_vs(self, subject_value_per_sqft: float | None) -> float | None:
        own = self.value_per_sqft
        if own is None or subject_value_per_sqft is None:
            return None
        return own - subject_value_per_sqft


# --------------------------------------------------------------------------- county description


@dataclass(frozen=True)
class CountyProfile:
    """Everything the shared pages need to know about how a county presents itself."""

    slug: str
    #: "Harris County" — used in headings and prose.
    display_name: str
    #: "HCAD" — the appraisal district's abbreviation.
    district_abbr: str
    #: "Harris County Appraisal District" — spelled out in the disclaimer.
    district_name: str
    #: What a property is called here: "Account" for Harris, "Property ID" for Brazos.
    key_label: str
    #: URL path this county is mounted at, e.g. "" or "brazos/".
    url_prefix: str
    #: Prefix applied to this county's URL names, e.g. "" or "brazos_".
    url_name_prefix: str

    search_fields: Sequence[SearchField]
    search_columns: Sequence[Column]
    comp_columns: Sequence[Column]

    #: Columns for the search-results CSV. Defaults to ``search_columns``; declare
    #: this when the export should be wider or more explicitly labelled than the
    #: on-screen table (which merges bed/bath into one cell, for instance).
    export_columns: Sequence[Column] = ()

    search_blurb: str = ""
    #: Optional amber caveat shown above the search form (data-coverage warnings).
    search_notice: str = ""

    #: Search fields that count as "meaningful" for a bulk CSV export.
    export_text_filters: Sequence[str] = ()
    export_zip_filter: str | None = None
    #: Whether this county can export the full search result set to CSV.
    supports_search_export: bool = True

    def url_name(self, view: str) -> str:
        """Reverse-able URL name for one of this county's pages."""
        return f"{self.url_name_prefix}{view}"

    @property
    def csv_columns(self) -> Sequence[Column]:
        return self.export_columns or self.search_columns


# --------------------------------------------------------------------------- adapter


class CountyAdapter(ABC):
    """What a county app implements to get the shared pages.

    Implementations translate between their own models and the neutral records
    above. They do no presentation work beyond that — equity maths, charts,
    recommendations, CSV, and PDF all live in the shared layer so every county
    produces the same analysis from the same inputs.
    """

    #: How this county's pages are labelled and columned.
    profile: CountyProfile

    # -- search ------------------------------------------------------------

    @abstractmethod
    def search_queryset(self, params: Mapping[str, str]):
        """Ordered queryset for the search form's ``params`` (already stripped)."""

    @abstractmethod
    def search_rows(self, records: Sequence[Any]) -> list[dict[str, Any]]:
        """Turn one page of ``search_queryset`` records into template rows.

        Each row's keys must cover ``profile.search_columns``. Rows may also
        carry ``similar_url`` and ``protest_url``; omit or set to ``None`` when
        a property lacks the data those pages need.
        """

    def search_context(self, params: Mapping[str, str]) -> dict[str, Any]:
        """Extra context for the search page (e.g. the active tax year)."""
        return {}

    # -- subject and comparables -------------------------------------------

    @abstractmethod
    def get_subject(self, key: str) -> Subject | None:
        """The property identified by ``key``, or ``None`` if it does not exist."""

    @abstractmethod
    def find_comps(
        self,
        key: str,
        *,
        max_distance_miles: float,
        max_results: int,
        min_score: float,
    ) -> list[Comp]:
        """Comparable properties for ``key``, best match first."""

    # -- optional enrichment ------------------------------------------------

    def assessment_history(self, key: str, limit: int = 5) -> list[dict[str, Any]]:
        """Per-year assessed values, newest first. Empty when the county has none."""
        return []

    def tax_impact(self, key: str, tax_year: int | None, median_assessed_value: Decimal | None):
        """Estimated tax impact, or ``None`` when the county cannot compute one."""
        return None
