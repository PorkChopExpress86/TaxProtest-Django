from typing import Dict

from django.db.models import QuerySet

from .models import PropertyRecord


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


def build_property_search_queryset(params: Dict[str, str]) -> QuerySet:
    """Return a filtered and ordered PropertyRecord queryset based on search params."""

    qs = PropertyRecord.objects.all()

    address = params.get("address", "").strip()
    street_name = params.get("street_name", "").strip()
    zip_code = params.get("zip_code", "").strip()
    last_name = params.get("last_name", "").strip()
    first_name = params.get("first_name", "").strip()

    if address:
        qs = qs.filter(address__icontains=address)
    if street_name:
        qs = qs.filter(street_name__icontains=street_name)
    if zip_code:
        qs = qs.filter(zipcode__icontains=zip_code)
    if last_name:
        qs = qs.filter(owner_name__icontains=last_name)
    if first_name:
        qs = qs.filter(owner_name__icontains=first_name)

    sort = params.get("sort", "zipcode")
    direction = params.get("dir", "asc")
    primary = SORT_MAP.get(sort, "zipcode")
    prefix = "-" if direction == "desc" else ""

    return qs.order_by(f"{prefix}{primary}", "street_number", "street_name")