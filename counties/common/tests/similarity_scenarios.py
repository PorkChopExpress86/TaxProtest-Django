"""Comparable scenarios shared by the similarity golden tests of both counties.

Each builder writes one multi-candidate neighbourhood around a subject and
returns the subject key. The candidates are chosen to exercise the rules a
score-neutral refactor must keep: ranking and tie-breaks, the minimum score,
the search radius, each county's pre-filters, half-building and
building-free scoring, and (for Brazos) the comparables lookup's
eligibility filtering after truncation.

Every Brazos property gets its own improvement identifiers. BCAD repeats
``imp_id`` values across properties in real data, so a fixture that shares
identifiers would hide any scorer that keys building facts by ``imp_id`` alone.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from counties.brazos.models import (
    BrazosPropertySnapshot,
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
    SnapshotOutcome,
)
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord

BRAZOS_TAX_YEAR = 2025

HARRIS_LAT = Decimal("29.8000000")
HARRIS_LON = Decimal("-95.5000000")
BRAZOS_LAT = Decimal("30.6700000")
BRAZOS_LON = Decimal("-96.3700000")


# ---------------------------------------------------------------- Harris


def harris_property(
    account_number: str,
    *,
    lat_offset: str = "0",
    lon_offset: str = "0",
    building: dict | None = None,
    features: tuple[str, ...] = (),
    **overrides,
) -> PropertyRecord:
    fields = {
        "address": f"{account_number} Golden St",
        "city": "Houston",
        "zipcode": "77040",
        "owner_name": f"Owner {account_number}",
        "account_number": account_number,
        "street_number": account_number[-3:],
        "street_name": "Golden St",
        "assessed_value": Decimal("350000"),
        "building_area": Decimal("2200"),
        "land_area": Decimal("9000"),
        "latitude": HARRIS_LAT + Decimal(lat_offset),
        "longitude": HARRIS_LON + Decimal(lon_offset),
        "is_residential": True,
        "is_data_ready": True,
    }
    fields.update(overrides)
    record = PropertyRecord.objects.create(**fields)
    if building is not None:
        building_fields = {
            "building_number": 1,
            "building_type": "A1",
            "building_style": "TR",
            "building_class": "R1",
            "quality_code": "A",
            "condition_code": "B",
            "year_built": 2005,
            "effective_year": 2008,
            "heat_area": Decimal("2200"),
            "stories": Decimal("2.0"),
            "bedrooms": 4,
            "bathrooms": Decimal("2.5"),
            "is_active": True,
        }
        building_fields.update(building)
        BuildingDetail.objects.create(
            property=record, account_number=account_number, **building_fields
        )
    for number, code in enumerate(features, start=1):
        ExtraFeature.objects.create(
            property=record,
            account_number=account_number,
            feature_number=number,
            feature_code=code,
            feature_description=f"{code} description",
            is_active=True,
        )
    return record


def build_harris_residential_scenario() -> str:
    """A residential Harris subject with ranked, tied and filtered-out candidates."""
    harris_property("HT0000000001", building={}, features=("POOL", "DETGAR"))

    # Near-identical, closest.
    harris_property(
        "HC0000000001",
        lat_offset="0.002",
        building={"heat_area": Decimal("2150")},
        features=("POOL",),
    )
    # Moderate differences on every residential factor, between curve knots.
    harris_property(
        "HC0000000002",
        lat_offset="0.010",
        lon_offset="0.010",
        land_area=Decimal("12000"),
        building={
            "heat_area": Decimal("2640"),
            "bedrooms": 3,
            "bathrooms": Decimal("2.0"),
            "quality_code": "B",
            "condition_code": "D",
            "year_built": 1994,
            "effective_year": 1996,
            "stories": Decimal("1.0"),
            "building_style": "TX",
        },
        features=("DETGAR", "SHED"),
    )
    # Two twins at the same distance and score: the account key breaks the tie.
    for account_number, lat_offset in (("HC0000000004", "0.005"), ("HC0000000003", "-0.005")):
        harris_property(
            account_number,
            lat_offset=lat_offset,
            building={
                "heat_area": Decimal("1900"),
                "condition_code": "",
                "year_remodeled": 2001,
                "effective_year": None,
                "building_style": "",
                "building_type": "A2",
            },
        )
    # Condition codes outside the quality rank fall back to categorical matching.
    harris_property(
        "HC0000000005",
        lat_offset="0.030",
        lon_offset="-0.020",
        land_area=None,
        building={
            "heat_area": Decimal("2420"),
            "condition_code": "BX",
            "quality_code": "",
            "stories": Decimal("1.5"),
            "bathrooms": Decimal("3.0"),
        },
        features=("POOL", "DETGAR"),
    )
    # Very different, still inside the living-area window: below the minimum score.
    harris_property(
        "HC0000000006",
        lat_offset="0.130",
        land_area=Decimal("30000"),
        building={
            "heat_area": Decimal("3300"),
            "bedrooms": 1,
            "bathrooms": Decimal("5.0"),
            "quality_code": "F",
            "condition_code": "F",
            "year_built": 1950,
            "effective_year": 1950,
            "stories": Decimal("1.0"),
            "building_style": "ZZ",
            "building_type": "ZZ",
            "building_class": "ZZ",
        },
        features=("BARN",),
    )
    # Outside the living-area window (50%-150% of the subject's heated area).
    harris_property("HX0000000001", lat_offset="0.001", building={"heat_area": Decimal("1000")})
    # Not data-ready, and not residential.
    harris_property("HX0000000002", lat_offset="0.001", is_data_ready=False, building={})
    harris_property("HX0000000003", lat_offset="0.001", is_residential=False, building={})
    # Outside the ten-mile radius.
    harris_property("HX0000000004", lat_offset="0.200", building={})
    return "HT0000000001"


def build_harris_land_subject_scenario() -> str:
    """A Harris subject without a building: half-building and land-only scoring."""
    harris_property("HL0000000001", building=None, land_area=Decimal("8000"), features=("POOL",))

    # Candidate with a building against a building-less subject (half-building).
    harris_property("HL0000000002", lat_offset="0.004", land_area=Decimal("8800"), building={})
    # Neither side has a building: land-only weights.
    harris_property(
        "HL0000000003",
        lat_offset="-0.015",
        land_area=Decimal("6500"),
        building=None,
        features=("POOL", "SHED"),
    )
    harris_property("HL0000000004", lat_offset="0.060", land_area=Decimal("8100"), building=None)
    return "HL0000000001"


# ---------------------------------------------------------------- Brazos


def _brazos_snapshot() -> None:
    BrazosPropertySnapshot.objects.create(
        tax_year=BRAZOS_TAX_YEAR,
        outcome=SnapshotOutcome.COMPLETED,
        cad_source_year=BRAZOS_TAX_YEAR,
        gis_source_year=BRAZOS_TAX_YEAR,
    )


def brazos_property(
    prop_id: str,
    *,
    lat_offset: str = "0",
    lon_offset: str = "0",
    improvements: tuple[dict, ...] = (),
    acreage: tuple[str, ...] = (),
    features: tuple[str, ...] = (),
    **overrides,
) -> PropertyAccount:
    """Write one Brazos property.

    Each improvement dict may carry ``building`` (characteristics fields, or
    absent for none) and ``second_floor`` (a SECOND FLOOR detail row). Its
    ``imp_id`` is always derived from the property, so identifiers never repeat.
    """
    fields = {
        "prop_id": prop_id,
        "tax_year": BRAZOS_TAX_YEAR,
        "owner_name": f"OWNER {prop_id}",
        "situs_address": f"{prop_id[-3:]} GOLDEN ST",
        "situs_city": "BRYAN",
        "situs_zip": "77801",
        "assessed_value": Decimal("300000"),
        "living_area": Decimal("2200"),
        "latitude": BRAZOS_LAT + Decimal(lat_offset),
        "longitude": BRAZOS_LON + Decimal(lon_offset),
        "coordinate_source": "bcad-certified-gis",
        "coordinate_source_year": BRAZOS_TAX_YEAR,
        "class_code": "RV3",
        "year_built": 2005,
    }
    fields.update(overrides)
    account = PropertyAccount.objects.create(**fields)

    for seq, spec in enumerate(improvements, start=1):
        imp_id = f"IMP-{prop_id}-{seq}"
        PropertyImprovement.objects.create(
            prop_id=prop_id,
            imp_id=imp_id,
            tax_year=BRAZOS_TAX_YEAR,
            improvement_type=spec.get("improvement_type", "R"),
            year_built=spec.get("year_built"),
        )
        if "building" in spec:
            building_fields = {
                "bedrooms": 4,
                "bathrooms": Decimal("2.5"),
                "exterior_wall": "BV",
            }
            building_fields.update(spec["building"])
            PropertyBuildingCharacteristic.objects.create(
                prop_id=prop_id, imp_id=imp_id, tax_year=BRAZOS_TAX_YEAR, **building_fields
            )
        PropertyImprovementDetail.objects.create(
            prop_id=prop_id,
            imp_id=imp_id,
            tax_year=BRAZOS_TAX_YEAR,
            detail_seq=1,
            detail_description="MAIN AREA",
        )
        if spec.get("second_floor"):
            PropertyImprovementDetail.objects.create(
                prop_id=prop_id,
                imp_id=imp_id,
                tax_year=BRAZOS_TAX_YEAR,
                detail_seq=2,
                detail_description="SECOND FLOOR",
            )

    for seq, value in enumerate(acreage, start=1):
        PropertyLand.objects.create(
            prop_id=prop_id, tax_year=BRAZOS_TAX_YEAR, land_seq=seq, acreage=Decimal(value)
        )
    for seq, feature_type in enumerate(features, start=1):
        PropertyExtraFeature.objects.create(
            prop_id=prop_id,
            imp_id=f"IMP-{prop_id}-1",
            tax_year=BRAZOS_TAX_YEAR,
            detail_seq=seq,
            feature_type=feature_type,
        )
    return account


def assert_brazos_improvement_ids_distinct() -> None:
    """Fail when any Brazos improvement identifier belongs to two properties."""
    owners: dict[str, set[str]] = defaultdict(set)
    for model in (
        PropertyImprovement,
        PropertyBuildingCharacteristic,
        PropertyImprovementDetail,
        PropertyExtraFeature,
    ):
        for prop_id, imp_id in model.objects.values_list("prop_id", "imp_id"):
            owners[imp_id].add(prop_id)
    repeated = {imp_id: sorted(props) for imp_id, props in owners.items() if len(props) > 1}
    if repeated:
        raise AssertionError(f"Improvement identifiers repeat across properties: {repeated}")


def build_brazos_residential_scenario() -> str:
    """A residential Brazos subject with ranked, ineligible and filtered-out candidates."""
    _brazos_snapshot()
    brazos_property(
        "BT0000000001",
        improvements=({"year_built": 2006, "building": {}, "second_floor": True},),
        acreage=("0.25",),
        features=("Fireplace", "Carport"),
    )

    # Near-identical, closest.
    brazos_property(
        "BC0000000001",
        lat_offset="0.002",
        living_area=Decimal("2150"),
        improvements=({"year_built": 2004, "building": {}, "second_floor": True},),
        acreage=("0.27",),
        features=("Fireplace",),
    )
    # Moderate differences; improvement has no year, so the account year applies.
    brazos_property(
        "BC0000000002",
        lat_offset="0.010",
        lon_offset="0.010",
        living_area=Decimal("2640"),
        class_code="RV4",
        year_built=1994,
        improvements=(
            {"building": {"bedrooms": 3, "bathrooms": Decimal("2.0"), "exterior_wall": "BR"}},
        ),
        acreage=("0.30", "0.10"),
        features=("Carport", "Shed"),
    )
    # Several improvements: the first residential one with characteristics wins.
    brazos_property(
        "BC0000000003",
        lat_offset="-0.005",
        living_area=Decimal("1900"),
        class_code="RF2P",
        improvements=(
            {"improvement_type": "M", "building": {"bedrooms": 9}},
            {"year_built": 1999},
            {
                "year_built": 1998,
                "building": {
                    "bedrooms": 3,
                    "bathrooms": Decimal("2.0"),
                    "exterior_wall": "",
                    "construction_style": "FR",
                },
                "second_floor": True,
            },
        ),
        acreage=("0.22",),
    )
    # High-ranking but lacks coordinate provenance: searched, not comparable-ready.
    brazos_property(
        "BC0000000004",
        lat_offset="0.003",
        coordinate_source="",
        improvements=({"year_built": 2006, "building": {}, "second_floor": True},),
        acreage=("0.25",),
        features=("Fireplace", "Carport"),
    )
    # Land mode (no living area): searched, but a different comparison mode.
    brazos_property(
        "BC0000000005",
        lat_offset="0.006",
        living_area=None,
        acreage=("0.26",),
    )
    # Residential with no land rows and no characteristics row.
    brazos_property(
        "BC0000000006",
        lat_offset="0.020",
        lon_offset="-0.015",
        living_area=Decimal("2450"),
        improvements=({"year_built": 2010},),
    )
    # Very different: below the minimum score.
    brazos_property(
        "BC0000000007",
        lat_offset="0.130",
        living_area=Decimal("4500"),
        class_code="RV9",
        year_built=1950,
        improvements=(
            {
                "year_built": 1950,
                "building": {
                    "bedrooms": 1,
                    "bathrooms": Decimal("5.0"),
                    "exterior_wall": "ZZ",
                },
            },
        ),
        acreage=("4.00",),
        features=("Barn",),
    )
    # Outside the ten-mile radius.
    brazos_property(
        "BX0000000001",
        lat_offset="0.200",
        improvements=({"building": {}},),
        acreage=("0.25",),
    )
    # A different tax year is never searched.
    brazos_property(
        "BX0000000002",
        lat_offset="0.001",
        tax_year=BRAZOS_TAX_YEAR - 1,
    )
    assert_brazos_improvement_ids_distinct()
    return "BT0000000001"


def build_brazos_building_free_scenario() -> str:
    """A Brazos subject without a characteristics row: building-free scoring."""
    _brazos_snapshot()
    brazos_property(
        "BF0000000001",
        improvements=({"year_built": 2006},),
        acreage=("0.25",),
        features=("Fireplace",),
    )
    # Neither side has a characteristics row: land-only weights despite living area.
    brazos_property(
        "BF0000000002",
        lat_offset="0.004",
        living_area=Decimal("3100"),
        class_code="RV6",
        improvements=({"year_built": 1980},),
        acreage=("0.31",),
        features=("Fireplace", "Carport"),
    )
    brazos_property(
        "BF0000000003",
        lat_offset="-0.012",
        acreage=("0.25",),
    )
    # The candidate has a characteristics row and the subject does not.
    brazos_property(
        "BF0000000004",
        lat_offset="0.008",
        living_area=Decimal("2300"),
        improvements=({"year_built": 2001, "building": {}, "second_floor": True},),
        acreage=("0.20",),
    )
    assert_brazos_improvement_ids_distinct()
    return "BF0000000001"
