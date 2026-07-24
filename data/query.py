from decimal import Decimal, InvalidOperation

from django.db.models import Exists, OuterRef, Q, QuerySet

from .models import BuildingDetail, PropertyRecord

SORT_MAP = {
    "zipcode": "zipcode",
    "street_number": "street_number",
    "street_name": "street_name",
    "owner_name": "owner_name",
    "value": "value",
    "assessed_value": "assessed_value",
    "building_area": "building_area",
    "land_area": "land_area",
}

# Advanced search fields, keyed to the GET param name used on the form. All are
# optional and additive to the basic owner/street/zip filters.
ADVANCED_SEARCH_FIELDS = (
    "min_value",
    "max_value",
    "min_sqft",
    "max_sqft",
    "min_bedrooms",
    "min_bathrooms",
    "min_year_built",
    "max_year_built",
)


def _parse_decimal(value: str) -> Decimal | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _parse_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _street_address_filter(street_address: str) -> Q:
    """Match a free-form street address against the parsed number/name columns.

    A user may enter just a house number, just a street name, or both in
    either order (e.g. "5712 Main St"). Some street names are themselves
    numeric (e.g. "FM 1960"), so we can't assume the first token is the
    house number. Instead, every whitespace-separated token must appear
    somewhere across street_number, street_name, or the raw address.
    """
    condition = Q()
    for token in street_address.split():
        condition &= (
            Q(street_number__icontains=token)
            | Q(street_name__icontains=token)
            | Q(address__icontains=token)
        )
    return condition


def _apply_advanced_filters(qs: QuerySet, params: dict[str, str]) -> QuerySet:
    min_value = _parse_decimal(params.get("min_value", ""))
    max_value = _parse_decimal(params.get("max_value", ""))
    if min_value is not None:
        qs = qs.filter(assessed_value__gte=min_value)
    if max_value is not None:
        qs = qs.filter(assessed_value__lte=max_value)

    building_filter = Q(is_active=True)
    has_building_filter = False

    min_sqft = _parse_int(params.get("min_sqft", ""))
    if min_sqft is not None:
        building_filter &= Q(heat_area__gte=min_sqft)
        has_building_filter = True
    max_sqft = _parse_int(params.get("max_sqft", ""))
    if max_sqft is not None:
        building_filter &= Q(heat_area__lte=max_sqft)
        has_building_filter = True

    min_bedrooms = _parse_int(params.get("min_bedrooms", ""))
    if min_bedrooms is not None:
        building_filter &= Q(bedrooms__gte=min_bedrooms)
        has_building_filter = True

    min_bathrooms = _parse_decimal(params.get("min_bathrooms", ""))
    if min_bathrooms is not None:
        building_filter &= Q(bathrooms__gte=min_bathrooms)
        has_building_filter = True

    min_year_built = _parse_int(params.get("min_year_built", ""))
    if min_year_built is not None:
        building_filter &= Q(year_built__gte=min_year_built)
        has_building_filter = True
    max_year_built = _parse_int(params.get("max_year_built", ""))
    if max_year_built is not None:
        building_filter &= Q(year_built__lte=max_year_built)
        has_building_filter = True

    if has_building_filter:
        qs = qs.filter(
            Exists(BuildingDetail.objects.filter(building_filter, property=OuterRef("pk")))
        )

    return qs


def build_property_search_queryset(params: dict[str, str]) -> QuerySet:
    """Return a filtered and ordered PropertyRecord queryset based on search params."""

    qs = PropertyRecord.objects.all()

    sort = params.get("sort", "zipcode")
    direction = params.get("dir", "asc")
    primary = SORT_MAP.get(sort, "zipcode")
    prefix = "-" if direction == "desc" else ""
    ordering = (f"{prefix}{primary}", "street_number", "street_name")

    account_number = params.get("account_number", "").strip()
    if account_number:
        return qs.filter(account_number__icontains=account_number).order_by(*ordering)

    owner_name = params.get("owner_name", "").strip()
    street_address = params.get("street_address", "").strip()
    zip_code = params.get("zip_code", "").strip()

    if owner_name:
        qs = qs.filter(owner_name__icontains=owner_name)
    if street_address:
        qs = qs.filter(_street_address_filter(street_address))
    if zip_code:
        qs = qs.filter(zipcode__icontains=zip_code)

    qs = _apply_advanced_filters(qs, params)

    return qs.order_by(*ordering)
