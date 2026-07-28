"""A source-neutral view of a property, so similarity scoring works on any county.

The scoring in `data.similarity` used to read HCAD's models directly — `heat_area`
off a BuildingDetail, `feature_code` off an ExtraFeature. Brazos CAD models the
same facts completely differently: living area is the sum of MA/MA2 improvement
detail rows, "features" are the other detail type codes, and there are no room
counts at all.

`ComparableProperty` is the common shape both counties are mapped onto, and a
`ComparableSource` knows how to build them for one county. Scoring then depends
only on the dataclass, so adding a third district means writing one source rather
than touching the algorithm.

Missing attributes are represented as None rather than zero. `data.similarity`
renormalises over whatever is present, so a county that cannot supply bedrooms
still scores on everything else — a zero would instead read as "0 bedrooms" and
actively distort the result.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import cos, radians
from typing import Protocol

from django.db.models import ExpressionWrapper, F, FloatField, Value
from django.db.models.functions import ACos, Cos, Greatest, Least, Radians, Sin

from data.models import (
    BuildingDetail,
    ExtraFeature,
    PropertyAccount,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyRecord,
)

# Brazos improvement-detail codes that make up heated living area. Everything
# else (OP open porch, AG attached garage, SP swimming pool, ...) is treated as
# a feature, which matches how HCAD splits BuildingDetail from ExtraFeature.
BRAZOS_LIVING_AREA_PREFIX = "MA"

# Candidates pulled from the database before Python-side scoring.
CANDIDATE_LIMIT = 2000

SQFT_PER_ACRE = 43560.0

# Search sort keys mapped onto Brazos columns. Keys match HCAD's SORT_MAP so the
# same sort links work whichever county is being viewed.
BRAZOS_SORT_MAP = {
    "zipcode": "situs_zip",
    "street_number": "situs_num",
    "street_name": "situs_street",
    "owner_name": "owner_name",
    "value": "assessed_val_prod_loss",
    "assessed_value": "assessed_val_prod_loss",
    "building_area": "prop_id",  # no denormalised living area on the account row
    "land_area": "land_acres",
}


@dataclass(frozen=True)
class ComparableProperty:
    """One property, normalised across counties.

    `source` is the underlying model instance and is handed back to callers so
    views and templates keep access to county-specific fields.
    """

    key: str
    source: object
    county: str = ""
    """Source name, e.g. "hcad" or "brazos"."""

    county_label: str = ""
    """Display name, e.g. "Harris"."""

    # Identity and headline figures, so views can render either county without
    # reaching back into a county-specific model.
    owner_name: str = ""
    street_number: str = ""
    street_name: str = ""
    zipcode: str = ""
    assessed_value: object | None = None

    latitude: float | None = None
    longitude: float | None = None

    land_area: float | None = None
    """Land size. Units only need to be consistent within one county, since
    every comparison is target-versus-candidate from the same source."""

    living_area: float | None = None
    bedrooms: float | None = None
    bathrooms: float | None = None
    quality_code: str = ""
    condition_code: str = ""
    stories: float | None = None
    effective_year: int | None = None
    character_codes: tuple[str, ...] = ()
    """Building type/style/class codes, most specific first."""

    feature_codes: frozenset[str] = field(default_factory=frozenset)
    feature_labels: tuple[str, ...] = ()
    has_building: bool = False
    """False means land-only, which switches scoring to LAND_ONLY_WEIGHTS."""

    building: object | None = None
    """Underlying building row (HCAD BuildingDetail; None for Brazos, which has
    no single building record). Carried so existing views keep working."""

    features: tuple = ()
    """Underlying feature rows, in the county's own model type."""

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def full_address(self) -> str:
        return " ".join(part for part in (self.street_number, self.street_name) if part).strip()

    @property
    def price_per_sqft(self) -> float | None:
        """Assessed value per square foot of living area."""
        if not self.assessed_value or not self.living_area:
            return None
        try:
            return float(self.assessed_value) / float(self.living_area)
        except (TypeError, ValueError, ZeroDivisionError):
            return None


def _f(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ComparableSource(Protocol):
    """Maps one county's models onto ComparableProperty."""

    name: str

    def get_target(self, key: str) -> ComparableProperty | None: ...

    def find_candidates(
        self, target: ComparableProperty, max_distance_miles: float
    ) -> list[tuple[ComparableProperty, float]]: ...


def _bounding_box(lat: float, lon: float, miles: float) -> tuple[float, float, float, float]:
    """Latitude/longitude box enclosing a radius, for an index-backed prefilter."""
    lat_range = miles / 69.0
    # Degrees of longitude shrink toward the poles.
    lon_range = miles / (69.0 * max(cos(radians(lat)), 1e-6))
    return lat - lat_range, lat + lat_range, lon - lon_range, lon + lon_range


def _annotate_distance(queryset, lat: float, lon: float):
    """Great-circle distance in miles, computed in the database."""
    lat_rad, lon_rad = radians(lat), radians(lon)
    return queryset.annotate(
        distance=ExpressionWrapper(
            3959.0
            * ACos(
                Least(
                    1.0,
                    Greatest(
                        -1.0,
                        Cos(Value(lat_rad))
                        * Cos(Radians(F("latitude")))
                        * Cos(Radians(F("longitude")) - Value(lon_rad))
                        + Sin(Value(lat_rad)) * Sin(Radians(F("latitude"))),
                    ),
                )
            ),
            output_field=FloatField(),
        )
    )


# ---------------------------------------------------------------------------
# Harris County (HCAD)
# ---------------------------------------------------------------------------


def hcad_comparable(
    record: PropertyRecord,
    building: BuildingDetail | None,
    features: list[ExtraFeature] | None,
) -> ComparableProperty:
    """Map HCAD's PropertyRecord/BuildingDetail/ExtraFeature onto the common shape."""
    effective_year = None
    if building is not None:
        for attr in ("effective_year", "year_remodeled", "year_built"):
            if value := getattr(building, attr, None):
                effective_year = int(value)
                break

    character = ()
    if building is not None:
        character = tuple(
            str(code)
            for code in (
                building.building_style,
                building.building_type,
                building.building_class,
            )
            if code
        )

    feature_list = features or []
    return ComparableProperty(
        key=record.account_number,
        source=record,
        county="hcad",
        county_label="Harris",
        owner_name=record.owner_name or "",
        street_number=record.street_number or "",
        street_name=record.street_name or "",
        zipcode=record.zipcode or "",
        assessed_value=record.assessed_value or record.value,
        latitude=_f(record.latitude),
        longitude=_f(record.longitude),
        land_area=_f(record.land_area),
        living_area=_f(getattr(building, "heat_area", None)),
        bedrooms=_f(getattr(building, "bedrooms", None)),
        bathrooms=_f(getattr(building, "bathrooms", None)),
        quality_code=str(getattr(building, "quality_code", "") or ""),
        condition_code=str(getattr(building, "condition_code", "") or ""),
        stories=_f(getattr(building, "stories", None)),
        effective_year=effective_year,
        character_codes=character,
        feature_codes=frozenset(f.feature_code for f in feature_list if f.feature_code),
        feature_labels=tuple(
            f.feature_description or f.feature_code for f in feature_list if f.feature_code
        ),
        has_building=building is not None,
        building=building,
        features=tuple(feature_list),
    )


class HcadSource:
    """PropertyRecord-backed comparables (Harris County)."""

    name = "hcad"
    label = "Harris"

    def search(self, params: dict[str, str]):
        """Filtered PropertyRecord queryset for the search page."""
        from data.query import build_property_search_queryset

        return build_property_search_queryset(params)

    def comparables_for(self, records: list[PropertyRecord]) -> list[ComparableProperty]:
        """Map a page of search results, fetching related rows in bulk."""
        accounts = [r.account_number for r in records]
        buildings: dict[str, BuildingDetail] = {}
        for building in BuildingDetail.objects.filter(
            account_number__in=accounts, is_active=True
        ).order_by("id"):
            buildings.setdefault(building.account_number, building)

        features: dict[str, list[ExtraFeature]] = defaultdict(list)
        for feature in ExtraFeature.objects.filter(
            account_number__in=accounts, is_active=True
        ).order_by("feature_description", "feature_code", "id"):
            features[feature.account_number].append(feature)

        return [
            hcad_comparable(r, buildings.get(r.account_number), features.get(r.account_number, []))
            for r in records
        ]

    def get_target(self, key: str) -> ComparableProperty | None:
        record = PropertyRecord.objects.filter(account_number=key).first()
        if record is None:
            return None
        building = record.buildings.filter(is_active=True).first()  # type: ignore[attr-defined]
        features = list(record.extra_features.filter(is_active=True))  # type: ignore[attr-defined]
        return hcad_comparable(record, building, features)

    def find_candidates(
        self, target: ComparableProperty, max_distance_miles: float
    ) -> list[tuple[ComparableProperty, float]]:
        assert target.latitude is not None and target.longitude is not None
        min_lat, max_lat, min_lon, max_lon = _bounding_box(
            target.latitude, target.longitude, max_distance_miles
        )

        queryset = PropertyRecord.objects.filter(
            latitude__gte=min_lat,
            latitude__lte=max_lat,
            longitude__gte=min_lon,
            longitude__lte=max_lon,
            latitude__isnull=False,
            longitude__isnull=False,
        ).exclude(account_number=target.key)

        # Narrow to plausible sizes before scoring; halves the Python-side work
        # on dense urban blocks.
        if target.living_area:
            matching = BuildingDetail.objects.filter(
                is_active=True,
                heat_area__gte=target.living_area * 0.5,
                heat_area__lte=target.living_area * 1.5,
            ).values("account_number")
            queryset = queryset.filter(account_number__in=matching)

        queryset = (
            _annotate_distance(queryset, target.latitude, target.longitude)
            .filter(distance__lte=max_distance_miles)
            .order_by("distance")[:CANDIDATE_LIMIT]
        )

        records = list(queryset)
        if not records:
            return []

        accounts = [r.account_number for r in records]
        buildings = {
            b.account_number: b
            for b in BuildingDetail.objects.filter(account_number__in=accounts, is_active=True)
        }
        features: dict[str, list[ExtraFeature]] = defaultdict(list)
        for feature in ExtraFeature.objects.filter(account_number__in=accounts, is_active=True):
            features[feature.account_number].append(feature)

        return [
            (
                hcad_comparable(
                    record,
                    buildings.get(record.account_number),
                    features.get(record.account_number, []),
                ),
                float(getattr(record, "distance", 0.0)),
            )
            for record in records
        ]


# ---------------------------------------------------------------------------
# Brazos County (BCAD)
# ---------------------------------------------------------------------------


def _brazos_building_facts(details: list[PropertyImprovementDetail]) -> dict[str, object]:
    """Fold improvement-detail rows into building-level attributes.

    Brazos has no single "building" row: a house is a set of detail records, one
    per area type. Living area is the MA* rows summed (MA main area, MA2 second
    floor); the rest describe garages, porches and pools and are treated as
    features. Quality and year are taken from the largest living-area row, which
    is the primary dwelling when a parcel holds more than one.
    """
    living_ids = {
        id(d)
        for d in details
        if (d.imprv_det_type_cd or "").upper().startswith(BRAZOS_LIVING_AREA_PREFIX)
        and d.imprv_det_area
    }
    living_rows = [d for d in details if id(d) in living_ids]
    other_rows = [d for d in details if id(d) not in living_ids]

    living_area = sum(float(d.imprv_det_area) for d in living_rows) or None
    primary = max(living_rows, key=lambda d: float(d.imprv_det_area), default=None)

    effective_year = None
    for row in (primary, *living_rows):
        if row is not None and row.yr_built:
            effective_year = int(row.yr_built)
            break

    # A recorded second-floor area means a multi-storey dwelling. This is the
    # only storey signal Brazos publishes; without it the factor would be unused.
    stories = None
    if living_rows:
        upper = {
            (d.imprv_det_type_cd or "").upper()
            for d in living_rows
            if (d.imprv_det_type_cd or "").upper() != "MA"
        }
        stories = 2.0 if upper else 1.0

    return {
        "living_area": living_area,
        "quality_code": (primary.imprv_det_class_cd or "") if primary else "",
        "effective_year": effective_year,
        "stories": stories,
        "feature_codes": frozenset(
            (d.imprv_det_type_cd or "").upper() for d in other_rows if d.imprv_det_type_cd
        ),
        "feature_labels": tuple(
            d.imprv_det_type_desc or d.imprv_det_type_cd for d in other_rows if d.imprv_det_type_cd
        ),
        "feature_rows": tuple(other_rows),
        "has_building": bool(living_rows),
    }


def brazos_comparable(
    account: PropertyAccount,
    details: list[PropertyImprovementDetail],
    improvements: list[PropertyImprovement] | None = None,
) -> ComparableProperty:
    """Map Brazos PropertyAccount + improvement details onto the common shape."""
    facts = _brazos_building_facts(details)

    land_area = None
    if account.land_acres:
        land_area = float(account.land_acres) * SQFT_PER_ACRE
    elif account.parcel_area_sqft:
        land_area = float(account.parcel_area_sqft)

    character = tuple(str(i.imprv_type_cd) for i in (improvements or []) if i.imprv_type_cd) or (
        (account.imprv_state_cd,) if account.imprv_state_cd else ()
    )

    street = " ".join(
        part
        for part in (account.situs_street_prefix, account.situs_street, account.situs_street_suffix)
        if part
    ).strip()
    if account.situs_unit:
        street = f"{street} {account.situs_unit}".strip()

    return ComparableProperty(
        key=str(account.prop_id),
        source=account,
        county="brazos",
        county_label="Brazos",
        owner_name=account.owner_name or "",
        street_number=account.situs_num or "",
        street_name=street,
        zipcode=account.situs_zip or "",
        # The post-productivity-loss figure, matching what the district publishes.
        assessed_value=account.assessed_value,
        latitude=_f(account.latitude),
        longitude=_f(account.longitude),
        land_area=land_area,
        living_area=facts["living_area"],  # type: ignore[arg-type]
        # Brazos publishes no room counts or condition ratings. Left as None so
        # scoring renormalises rather than treating them as zero.
        bedrooms=None,
        bathrooms=None,
        quality_code=str(facts["quality_code"]),
        condition_code="",
        stories=facts["stories"],  # type: ignore[arg-type]
        effective_year=facts["effective_year"],  # type: ignore[arg-type]
        character_codes=character,
        feature_codes=facts["feature_codes"],  # type: ignore[arg-type]
        feature_labels=facts["feature_labels"],  # type: ignore[arg-type]
        has_building=bool(facts["has_building"]),
        # Brazos has no single building row; the detail rows carry everything.
        building=None,
        features=facts["feature_rows"],  # type: ignore[arg-type]
    )


class BrazosSource:
    """PropertyAccount-backed comparables (Brazos County)."""

    name = "brazos"
    label = "Brazos"

    def __init__(self, tax_year: int | None = None) -> None:
        self.tax_year = tax_year

    def latest_year(self) -> int | None:
        if self.tax_year is not None:
            return self.tax_year
        return (
            PropertyAccount.objects.order_by("-tax_year").values_list("tax_year", flat=True).first()
        )

    def search(self, params: dict[str, str]):
        """Filtered PropertyAccount queryset for the search page.

        Brazos publishes no situs city and a ZIP on ~0.2% of rows, so a ZIP
        filter would silently exclude almost everything. It is applied only when
        the row actually carries one; the other filters map onto owner name and
        situs street.
        """
        year = self.latest_year()
        if year is None:
            return PropertyAccount.objects.none()

        queryset = PropertyAccount.objects.filter(tax_year=year)

        address = (params.get("address") or "").strip()
        street_name = (params.get("street_name") or "").strip()
        zip_code = (params.get("zip_code") or "").strip()
        last_name = (params.get("last_name") or "").strip()
        first_name = (params.get("first_name") or "").strip()

        if address:
            # The search box takes a whole address; match the street portion and,
            # when it starts with digits, the house number too.
            leading_number = address.split(" ")[0]
            if leading_number.isdigit():
                remainder = address[len(leading_number) :].strip()
                queryset = queryset.filter(situs_num=leading_number)
                if remainder:
                    queryset = queryset.filter(situs_street__icontains=remainder)
            else:
                queryset = queryset.filter(situs_street__icontains=address)
        if street_name:
            queryset = queryset.filter(situs_street__icontains=street_name)
        if zip_code:
            queryset = queryset.filter(situs_zip__icontains=zip_code)
        if last_name:
            queryset = queryset.filter(owner_name__icontains=last_name)
        if first_name:
            queryset = queryset.filter(owner_name__icontains=first_name)

        sort = params.get("sort", "zipcode")
        prefix = "-" if params.get("dir") == "desc" else ""
        primary = BRAZOS_SORT_MAP.get(sort, "situs_zip")
        return queryset.order_by(f"{prefix}{primary}", "situs_street", "situs_num")

    def comparables_for(self, accounts: list[PropertyAccount]) -> list[ComparableProperty]:
        """Map a page of search results, fetching improvement rows in bulk."""
        if not accounts:
            return []
        year = accounts[0].tax_year
        prop_ids = [a.prop_id for a in accounts]

        details: dict[int, list[PropertyImprovementDetail]] = defaultdict(list)
        for detail in PropertyImprovementDetail.objects.filter(prop_id__in=prop_ids, tax_year=year):
            details[detail.prop_id].append(detail)
        improvements: dict[int, list[PropertyImprovement]] = defaultdict(list)
        for improvement in PropertyImprovement.objects.filter(prop_id__in=prop_ids, tax_year=year):
            improvements[improvement.prop_id].append(improvement)

        return [
            brazos_comparable(a, details.get(a.prop_id, []), improvements.get(a.prop_id, []))
            for a in accounts
        ]

    def _resolve_year(self, prop_id: int) -> int | None:
        if self.tax_year is not None:
            return self.tax_year
        return (
            PropertyAccount.objects.filter(prop_id=prop_id)
            .order_by("-tax_year")
            .values_list("tax_year", flat=True)
            .first()
        )

    def get_target(self, key: str) -> ComparableProperty | None:
        try:
            prop_id = int(str(key).strip())
        except (TypeError, ValueError):
            return None

        year = self._resolve_year(prop_id)
        if year is None:
            return None

        account = PropertyAccount.objects.filter(prop_id=prop_id, tax_year=year).first()
        if account is None:
            return None
        self.tax_year = year

        details = list(PropertyImprovementDetail.objects.filter(prop_id=prop_id, tax_year=year))
        improvements = list(PropertyImprovement.objects.filter(prop_id=prop_id, tax_year=year))
        return brazos_comparable(account, details, improvements)

    def find_candidates(
        self, target: ComparableProperty, max_distance_miles: float
    ) -> list[tuple[ComparableProperty, float]]:
        assert target.latitude is not None and target.longitude is not None
        year = self.tax_year
        if year is None:
            return []

        min_lat, max_lat, min_lon, max_lon = _bounding_box(
            target.latitude, target.longitude, max_distance_miles
        )
        queryset = PropertyAccount.objects.filter(
            tax_year=year,
            latitude__gte=min_lat,
            latitude__lte=max_lat,
            longitude__gte=min_lon,
            longitude__lte=max_lon,
            latitude__isnull=False,
            longitude__isnull=False,
        ).exclude(prop_id=int(target.key))
        queryset = (
            _annotate_distance(queryset, target.latitude, target.longitude)
            .filter(distance__lte=max_distance_miles)
            .order_by("distance")[:CANDIDATE_LIMIT]
        )

        accounts = list(queryset)
        if not accounts:
            return []

        prop_ids = [a.prop_id for a in accounts]
        details: dict[int, list[PropertyImprovementDetail]] = defaultdict(list)
        for detail in PropertyImprovementDetail.objects.filter(prop_id__in=prop_ids, tax_year=year):
            details[detail.prop_id].append(detail)
        improvements: dict[int, list[PropertyImprovement]] = defaultdict(list)
        for improvement in PropertyImprovement.objects.filter(prop_id__in=prop_ids, tax_year=year):
            improvements[improvement.prop_id].append(improvement)

        return [
            (
                brazos_comparable(
                    account, details.get(account.prop_id, []), improvements.get(account.prop_id, [])
                ),
                float(getattr(account, "distance", 0.0)),
            )
            for account in accounts
        ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SOURCES: dict[str, type] = {"hcad": HcadSource, "brazos": BrazosSource}

# Static so rendering the search form costs no queries. A county with nothing
# loaded simply returns no rows.
COUNTY_CHOICES = [("", "All counties"), ("hcad", "Harris"), ("brazos", "Brazos")]


def search_comparables(
    params: dict[str, str], county: str | None = None, limit: int = 400
) -> list[ComparableProperty]:
    """Search one county, or all of them, returning normalised rows.

    Capped per county so an unfiltered search cannot pull a whole roll into
    memory, and so one large county cannot crowd another out of the results.
    """
    names = [county] if county else list(SOURCES)

    found: list[ComparableProperty] = []
    for name in names:
        try:
            source = get_source(name)
        except ValueError:
            continue
        page = list(source.search(params)[:limit])  # type: ignore[attr-defined]
        found.extend(source.comparables_for(page))  # type: ignore[attr-defined]
    return found


def get_source(name: str) -> ComparableSource:
    try:
        return SOURCES[name.lower()]()  # type: ignore[return-value]
    except KeyError:
        raise ValueError(
            f"Unknown comparable source {name!r}. Known: {', '.join(SOURCES)}"
        ) from None


def resolve_source(
    key: str, name: str | None = None
) -> tuple[ComparableSource, ComparableProperty] | None:
    """Find which county a key belongs to and load its target property.

    HCAD account numbers and Brazos property ids are both numeric strings, so an
    explicit `name` wins; otherwise HCAD is tried first to preserve the existing
    behaviour of every current caller.
    """
    if name:
        source = get_source(name)
        target = source.get_target(key)
        return (source, target) if target else None

    for candidate_name in ("hcad", "brazos"):
        source = get_source(candidate_name)
        target = source.get_target(key)
        if target is not None:
            return source, target
    return None
