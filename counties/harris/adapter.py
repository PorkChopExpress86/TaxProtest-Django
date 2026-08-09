"""Harris County (HCAD) bound to the shared county web layer.

Translates ``PropertyRecord`` / ``BuildingDetail`` / ``ExtraFeature`` into the
county-neutral records in :mod:`counties.common.contracts`. All analysis,
charting, CSV, and PDF work happens in the shared layer.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from django.urls import reverse

from counties.common.contracts import (
    Column,
    Comp,
    CountyAdapter,
    CountyProfile,
    SearchField,
    Subject,
)
from counties.common.history import assessment_history_rows
from counties.common.tax_impact import calculate_tax_impact
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord
from counties.harris.query import build_property_search_queryset
from counties.harris.similarity import (
    find_similar_properties,
    format_feature_list,
    get_similarity_label,
)

COUNTY_SLUG = "harris"

HARRIS_PROFILE = CountyProfile(
    slug=COUNTY_SLUG,
    display_name="Harris County",
    district_abbr="HCAD",
    district_name="Harris County Appraisal District",
    key_label="Account",
    url_prefix="",
    url_name_prefix="",
    search_blurb=(
        "Get accurate property valuations, tax assessments, and market insights for your home. "
        "Search Harris County Appraisal District records by owner name, address, or ZIP."
    ),
    search_fields=(
        SearchField("first_name", "First Name", placeholder="John"),
        SearchField("last_name", "Last Name", placeholder="Smith"),
        SearchField("address", "Address", placeholder="123"),
        SearchField("street_name", "Street Name", placeholder="Main Street"),
        SearchField("zip_code", "Zip Code", placeholder="77001", maxlength=5, pattern="[0-9]{5}"),
    ),
    search_columns=(
        Column("Acct #", "account_number", format="mono"),
        Column("Owner", "owner_name", sort_key="owner_name", truncate=True),
        Column("Street #", "address", sort_key="street_number"),
        Column("Street Name", "street_name", sort_key="street_name", truncate=True),
        Column("Zip", "zip_code", sort_key="zipcode"),
        Column(
            "Assessed Value",
            "assessed_value",
            align="right",
            format="currency",
            sort_key="assessed_value",
        ),
        Column(
            "Bldg Area", "building_area", align="right", format="sqft", sort_key="building_area"
        ),
        Column("Bed/Bath", "bedrooms", align="right", format="bedbath"),
        Column(
            "Quality",
            "quality_code",
            align="center",
            format="quality",
            title="Property Quality: X=Superior, A=Excellent, B=Good, C=Average, D=Low",
        ),
        Column("Features", "features", truncate=True),
        Column("$/SF", "ppsf", align="right", format="ppsf"),
    ),
    # The on-screen table merges bed/bath into one cell and abbreviates headers;
    # the CSV spells both out so downstream spreadsheets stay self-describing.
    export_columns=(
        Column("Account Number", "account_number"),
        Column("Owner Name", "owner_name"),
        Column("Street Number", "address"),
        Column("Street Name", "street_name"),
        Column("Zip Code", "zip_code"),
        Column("Assessed Value", "assessed_value", format="currency"),
        Column("Building Area (sqft)", "building_area", format="sqft"),
        Column("Bedrooms", "bedrooms", format="number"),
        Column("Bathrooms", "bathrooms", format="number"),
        Column("Quality", "quality_code"),
        Column("Features", "features"),
        Column("Price per sqft", "ppsf", format="ppsf"),
    ),
    comp_columns=(
        Column("Sqft", "living_area", align="right", format="sqft"),
        Column("Bed/Bath", "bedrooms", align="right", format="bedbath"),
        Column("Year", "year_built", align="right"),
        Column("Qual", "quality_code", align="center", format="quality"),
        Column("Assessed Value", "assessed_value", align="right", format="currency"),
    ),
    export_text_filters=("first_name", "last_name", "address", "street_name"),
    export_zip_filter="zip_code",
)


def _ppsf(value: Decimal | float | None, area: float | int | None) -> float | None:
    if not value or not area or float(area) <= 0:
        return None
    try:
        return float(value) / float(area)
    except (TypeError, ValueError, ArithmeticError):
        return None


class HarrisAdapter(CountyAdapter):
    profile = HARRIS_PROFILE

    # -- search ------------------------------------------------------------

    def search_queryset(self, params: Mapping[str, str]):
        return build_property_search_queryset(dict(params))

    def search_rows(self, records: Sequence[PropertyRecord]) -> list[dict[str, Any]]:
        buildings, features = self._related_maps(records)
        rows = []
        for prop in records:
            assessed = prop.assessed_value or prop.value
            building = buildings.get(prop.account_number)
            prop_features = features.get(prop.account_number, [])
            rows.append(
                {
                    "account_number": prop.account_number,
                    "owner_name": prop.owner_name,
                    "address": prop.street_number,
                    "street_name": prop.street_name,
                    "zip_code": prop.zipcode,
                    "assessed_value": assessed,
                    "building_area": prop.building_area,
                    "land_area": prop.land_area,
                    "ppsf": _ppsf(assessed, prop.building_area),
                    "bedrooms": building.bedrooms if building else None,
                    "bathrooms": building.bathrooms if building else None,
                    "quality_code": building.quality_code if building else None,
                    "features": (
                        format_feature_list(prop_features, max_features=5)
                        if prop_features
                        else None
                    ),
                    "similar_url": reverse("similar_properties", args=[prop.account_number]),
                }
            )
        return rows

    @staticmethod
    def _related_maps(records: Sequence[PropertyRecord]):
        """One query each for buildings and features across the whole page."""
        account_numbers = [prop.account_number for prop in records]

        buildings: dict[str, BuildingDetail] = {}
        for building in BuildingDetail.objects.filter(
            account_number__in=account_numbers, is_active=True
        ).order_by("id"):
            buildings.setdefault(building.account_number, building)

        features: dict[str, list[ExtraFeature]] = defaultdict(list)
        for feature in ExtraFeature.objects.filter(
            account_number__in=account_numbers, is_active=True
        ).order_by("feature_description", "feature_code", "id"):
            features[feature.account_number].append(feature)

        return buildings, features

    # -- subject and comparables -------------------------------------------

    def get_subject(self, key: str) -> Subject | None:
        # filter().first() rather than get(): legacy imports can leave duplicate
        # rows for one account, and the report only needs one of them.
        prop = PropertyRecord.objects.filter(account_number=key).first()
        if prop is None:
            return None

        building = prop.buildings.filter(is_active=True).first()
        features = list(prop.extra_features.filter(is_active=True))
        living_area = float(building.heat_area) if building and building.heat_area else None
        if living_area is None and prop.building_area:
            living_area = float(prop.building_area)

        locality = ", ".join(part for part in [prop.city, "TX"] if part)
        if prop.zipcode:
            locality = f"{locality} {prop.zipcode}".strip()

        return Subject(
            key=prop.account_number,
            address_line=f"{prop.street_number or ''} {prop.street_name or ''}".strip(),
            locality_line=locality,
            owner_name=prop.owner_name,
            zip_code=prop.zipcode,
            assessed_value=prop.assessed_value or prop.value,
            living_area=living_area,
            land_area=prop.land_area,
            bedrooms=building.bedrooms if building else None,
            bathrooms=building.bathrooms if building else None,
            year_built=building.year_built if building else None,
            quality_code=building.quality_code if building else None,
            features=format_feature_list(features),
            has_location=bool(prop.latitude and prop.longitude),
        )

    def find_comps(
        self, key: str, *, max_distance_miles: float, max_results: int, min_score: float
    ) -> list[Comp]:
        results = find_similar_properties(
            account_number=key,
            max_distance_miles=max_distance_miles,
            max_results=max_results,
            min_score=min_score,
        )

        comps = []
        for result in results:
            prop = result["property"]
            building = result["building"]
            features = result["features"]
            living_area = float(building.heat_area) if building and building.heat_area else None
            if living_area is None and prop.building_area:
                living_area = float(prop.building_area)

            comps.append(
                Comp(
                    key=prop.account_number,
                    address=f"{prop.street_number or ''} {prop.street_name or ''}".strip(),
                    zip_code=prop.zipcode,
                    owner_name=prop.owner_name,
                    assessed_value=prop.assessed_value or prop.value,
                    living_area=living_area,
                    land_area=prop.land_area,
                    bedrooms=building.bedrooms if building else None,
                    bathrooms=building.bathrooms if building else None,
                    year_built=building.year_built if building else None,
                    quality_code=building.quality_code if building else None,
                    condition_code=building.condition_code if building else None,
                    features=format_feature_list(features, max_features=5),
                    distance=result["distance"],
                    similarity_score=result["similarity_score"],
                    match_label=get_similarity_label(result["similarity_score"]),
                    score_breakdown=result.get("score_breakdown", []),
                )
            )
        return comps

    # -- enrichment ---------------------------------------------------------

    def assessment_history(self, key: str, limit: int = 5) -> list[dict[str, Any]]:
        return assessment_history_rows(key, county=COUNTY_SLUG, limit=limit)

    def tax_impact(self, key: str, tax_year: int | None, median_assessed_value: Decimal | None):
        return calculate_tax_impact(
            account_number=key,
            tax_year=tax_year,
            median_assessed_value=median_assessed_value,
            county=COUNTY_SLUG,
        )


adapter = HarrisAdapter()
