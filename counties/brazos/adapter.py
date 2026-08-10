"""Brazos County (BCAD) bound to the shared county web layer.

BCAD's PACS export is shaped nothing like HCAD's — ``PropertyAccount`` joins to
``PropertyLand`` / ``PropertyImprovement`` on a plain ``prop_id`` string, living
area comes off the account rather than a building row, and quality is a class
code rather than a letter grade. This adapter absorbs all of that so the shared
views and templates see the same neutral records they see for Harris.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from django.db.models import Max, Sum
from django.urls import reverse

from counties.brazos.models import (
    PropertyAccount,
    PropertyExtraFeature,
    PropertyLand,
)
from counties.brazos.similarity import (
    _primary_improvement,
    find_similar_properties,
    get_similarity_label,
)
from counties.common.contracts import (
    Column,
    Comp,
    CountyAdapter,
    CountyProfile,
    DetailRow,
    ScoreComponent,
    SearchField,
    Subject,
)
from counties.common.history import assessment_history_rows
from counties.common.tax_impact import calculate_tax_impact

COUNTY_SLUG = "brazos"

SORT_MAP = {
    "prop_id": "prop_id",
    "owner_name": "owner_name",
    "situs_city": "situs_city",
    "situs_zip": "situs_zip",
}

BRAZOS_PROFILE = CountyProfile(
    slug=COUNTY_SLUG,
    display_name="Brazos County",
    district_abbr="BCAD",
    district_name="Brazos Central Appraisal District",
    key_label="Property ID",
    url_prefix="brazos/",
    url_name_prefix="brazos_",
    search_blurb=(
        "Search Brazos Central Appraisal District (BCAD) property records by owner or address."
    ),
    search_notice=(
        "Partial data: Brazos County coverage is smaller than Harris County's — situs address, "
        "values, and building details come from BCAD's GIS parcel data, which doesn't cover every "
        "property (roughly half of residential improvements have full bed/bath/quality detail). "
        "Comparable and report links are only shown for properties with the location data a "
        "comps analysis needs."
    ),
    search_fields=(
        SearchField("owner_name", "Owner Name", placeholder="Smith"),
        SearchField("address", "Address", placeholder="123 Main St"),
        SearchField("zip_code", "Zip Code", placeholder="77801", maxlength=10),
    ),
    search_columns=(
        Column("Prop ID", "prop_id", format="mono", sort_key="prop_id"),
        Column("Owner", "owner_name", sort_key="owner_name", truncate=True),
        Column("Address", "address", truncate=True),
        Column("City", "city", sort_key="situs_city"),
        Column("Zip", "zip_code", sort_key="situs_zip"),
        Column("Land Value", "land_value", align="right", format="currency"),
        Column("Acreage", "acreage", align="right", format="acres"),
    ),
    comp_columns=(
        Column("Sqft", "living_area", align="right", format="sqft"),
        Column("Bed/Bath", "bedrooms", align="right", format="bedbath"),
        Column("Class", "quality_code", align="center"),
        Column("Assessed Value", "assessed_value", align="right", format="currency"),
    ),
    export_text_filters=("owner_name", "address"),
    export_zip_filter="zip_code",
)


def _format_feature_list(features: Sequence[PropertyExtraFeature], max_features: int = 10) -> str:
    """Brazos analog of Harris's ``format_feature_list``.

    BCAD stores feature_type/feature_value pairs with no separate description
    field, so the label is built from the pair rather than looked up.
    """
    counts: dict[str, int] = {}
    for feature in features:
        label = feature.feature_type or "Unknown"
        if feature.feature_value:
            label = f"{label} ({feature.feature_value})"
        counts[label] = counts.get(label, 0) + 1

    items = [
        f"{label} x{count}" if count > 1 else label
        for label, count in sorted(counts.items())[:max_features]
    ]
    return ", ".join(items) if items else "None"


class BrazosAdapter(CountyAdapter):
    profile = BRAZOS_PROFILE

    # -- search ------------------------------------------------------------

    def active_year(self) -> int | None:
        """Only one tax year is loaded at a time — load_brazos_cad replaces the prior year."""
        return PropertyAccount.objects.aggregate(Max("tax_year"))["tax_year__max"]

    def search_queryset(self, params: Mapping[str, str]):
        year = self.active_year()
        if not year:
            return PropertyAccount.objects.none()

        qs = PropertyAccount.objects.filter(tax_year=year)
        if params.get("owner_name"):
            qs = qs.filter(owner_name__icontains=params["owner_name"])
        if params.get("address"):
            qs = qs.filter(situs_address__icontains=params["address"])
        if params.get("zip_code"):
            qs = qs.filter(situs_zip__icontains=params["zip_code"])

        primary = SORT_MAP.get(params.get("sort", ""), "owner_name")
        prefix = "-" if params.get("dir") == "desc" else ""
        return qs.order_by(f"{prefix}{primary}", "prop_id")

    def search_context(self, params: Mapping[str, str]) -> dict[str, Any]:
        return {"active_year": self.active_year()}

    def search_rows(self, records: Sequence[PropertyAccount]) -> list[dict[str, Any]]:
        if not records:
            return []

        # No FK from PropertyAccount to PropertyLand (both key on a plain
        # prop_id string), so land totals are merged in manually.
        year = records[0].tax_year
        land_by_prop = {
            row["prop_id"]: row
            for row in PropertyLand.objects.filter(
                prop_id__in=[record.prop_id for record in records], tax_year=year
            )
            .values("prop_id")
            .annotate(total_land_value=Sum("land_value"), total_acreage=Sum("acreage"))
        }

        rows = []
        for account in records:
            land = land_by_prop.get(account.prop_id, {})
            has_location = bool(account.latitude and account.longitude)
            rows.append(
                {
                    "prop_id": account.prop_id,
                    "owner_name": account.owner_name,
                    "address": account.situs_address,
                    "city": account.situs_city,
                    "zip_code": account.situs_zip,
                    "land_value": land.get("total_land_value"),
                    "acreage": land.get("total_acreage"),
                    "similar_url": (
                        reverse("brazos_similar_properties", args=[account.prop_id])
                        if has_location
                        else None
                    ),
                    "protest_url": (
                        reverse("brazos_protest_analysis", args=[account.prop_id])
                        if has_location
                        else None
                    ),
                }
            )
        return rows

    # -- subject and comparables -------------------------------------------

    def get_subject(self, key: str) -> Subject | None:
        account = PropertyAccount.objects.filter(prop_id=key).order_by("-tax_year").first()
        if account is None:
            return None

        year = account.tax_year
        improvement, building = _primary_improvement(key, year)
        year_built = (
            improvement.year_built if improvement and improvement.year_built else account.year_built
        )
        features = list(PropertyExtraFeature.objects.filter(prop_id=key, tax_year=year))

        detail_rows = []
        if account.class_code:
            detail_rows.append(DetailRow("Class Code", account.class_code))

        locality = ", ".join(part for part in [account.situs_city, "TX"] if part)
        if account.situs_zip:
            locality = f"{locality} {account.situs_zip}".strip()

        return Subject(
            key=account.prop_id,
            address_line=account.situs_address or "Address not on file",
            locality_line=locality,
            owner_name=account.owner_name,
            zip_code=account.situs_zip,
            assessed_value=account.assessed_value,
            living_area=float(account.living_area) if account.living_area else None,
            bedrooms=building.bedrooms if building else None,
            bathrooms=building.bathrooms if building else None,
            year_built=year_built,
            features=_format_feature_list(features),
            has_location=bool(account.latitude and account.longitude),
            tax_year=year,
            detail_rows=detail_rows,
        )

    def find_comps(
        self, key: str, *, max_distance_miles: float, max_results: int, min_score: float
    ) -> list[Comp]:
        results = find_similar_properties(
            key,
            max_distance_miles=max_distance_miles,
            max_results=max_results,
            min_score=min_score,
        )

        comps = []
        for result in results:
            account = result["property"]
            building = result["building"]
            comps.append(
                Comp(
                    key=account.prop_id,
                    address=account.situs_address or account.prop_id,
                    zip_code=account.situs_zip,
                    owner_name=account.owner_name,
                    assessed_value=account.assessed_value,
                    living_area=float(account.living_area) if account.living_area else None,
                    bedrooms=building.bedrooms if building else None,
                    bathrooms=building.bathrooms if building else None,
                    # BCAD's class code plays the role Harris's quality grade does.
                    quality_code=account.class_code,
                    features=_format_feature_list(result["features"], max_features=5),
                    distance=result["distance"],
                    similarity_score=result["similarity_score"],
                    match_label=get_similarity_label(result["similarity_score"]),
                    score_breakdown=[
                        ScoreComponent.from_mapping(component)
                        for component in result.get("score_breakdown", [])
                    ],
                )
            )
        return comps

    # -- enrichment ---------------------------------------------------------

    def assessment_history(self, key: str, limit: int = 5) -> list[dict[str, Any]]:
        # No Brazos rows exist in the shared AssessmentHistory table yet; this
        # returns [] today and picks up real data automatically once an ingest
        # populates it, with no code change here.
        return assessment_history_rows(key, county=COUNTY_SLUG, limit=limit)

    def tax_impact(self, key: str, tax_year: int | None, median_assessed_value: Decimal | None):
        return calculate_tax_impact(
            account_number=key,
            tax_year=tax_year,
            median_assessed_value=median_assessed_value,
            county=COUNTY_SLUG,
        )


adapter = BrazosAdapter()
