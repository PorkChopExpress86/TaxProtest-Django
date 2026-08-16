"""Reading parcel coordinates.

``ParcelGeometry`` is a county-scoped table keyed by account number, and using
it correctly means knowing several things that are easy to get wrong and easy
to get *inconsistently* right:

- every query has to be scoped by ``county``; the table holds all of them
- ``latitude`` and ``longitude`` are independently nullable, so "a row exists"
  and "coordinates are known" are different questions
- rows are keyed by an account-number *string*, not a foreign key, so there is
  no ORM traversal from a property to its geometry
- the table holds every parcel in the source shapefile, including ones with no
  property behind them, so a bare geometry query is not a property query

Callers ask questions here instead of building those filters themselves. The
one caller left reaching past this module is the Postgres branch of
``etl_pipeline.readiness``, which needs the same predicate inside raw SQL, and
the two GIS loaders, which write rather than read.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from math import cos, radians

from django.db.models import (
    Exists,
    ExpressionWrapper,
    F,
    FloatField,
    OuterRef,
    QuerySet,
    Value,
)
from django.db.models.functions import ACos, Cos, Greatest, Least, Radians, Sin

from .tax_models import ParcelGeometry

# How many parcels a proximity search pulls out of the database to score in
# Python. Every one of these slots has to buy a usable candidate, which is why
# ``nearest_parcels`` requires ``backed_by`` rather than offering it.
MAX_NEARBY_PARCELS = 2000

# Miles per degree of latitude. Longitude degrees shrink with the cosine of the
# latitude, which is why the longitude span is scaled below.
_MILES_PER_DEGREE_LATITUDE = 69.0


@dataclass(frozen=True)
class Coordinates:
    """A parcel's location. Both values are always present."""

    latitude: Decimal
    longitude: Decimal


def coordinates_for(account_number: str, *, county: str) -> Coordinates | None:
    """Return the account's coordinates, or None if they aren't known.

    A missing row and a row whose latitude or longitude is NULL are the same
    answer to the caller, so both collapse to None.
    """
    row = (
        ParcelGeometry.objects.filter(
            account_number=account_number,
            county=county,
            latitude__isnull=False,
            longitude__isnull=False,
        )
        .values_list("latitude", "longitude")
        .first()
    )
    if row is None:
        return None
    return Coordinates(latitude=row[0], longitude=row[1])


def accounts_with_coordinates(account_numbers: Iterable[str], *, county: str) -> set[str]:
    """Return the subset of ``account_numbers`` that have known coordinates.

    Takes an explicit account list rather than offering to fetch every account
    in the county: the tables run to millions of rows, and materialising all of
    them into Python is a mistake this interface should not make available.
    Use :func:`coordinates_exist` to ask the same question in SQL.
    """
    accounts = list(account_numbers)
    if not accounts:
        return set()
    return set(
        ParcelGeometry.objects.filter(
            account_number__in=accounts,
            county=county,
            latitude__isnull=False,
            longitude__isnull=False,
        ).values_list("account_number", flat=True)
    )


def coordinates_exist(*, county: str, account_field: str = "account_number") -> Exists:
    """An ``Exists`` for "this row's account has coordinates", for use in a queryset.

    Deliberately an ``Exists`` rather than ``account_number__in=<queryset>``.
    Django renders the ``__in`` form as ``IN (subquery)`` and its negation as
    ``NOT IN (subquery)``; ``NOT IN`` has to honour SQL's three-valued logic, so
    Postgres cannot use a hash anti-join and re-checks the subquery per row.
    Measured against 1.17M properties and 1.55M parcels, the ``NOT IN`` form ran
    for over an hour without completing where ``NOT EXISTS`` returns in ~470ms.

    ``account_field`` names the column on the *outer* model holding the account
    number — ``account_number`` on Harris's PropertyRecord, ``prop_id`` on
    Brazos's PropertyAccount.
    """
    return Exists(
        ParcelGeometry.objects.filter(
            account_number=OuterRef(account_field),
            county=county,
            latitude__isnull=False,
            longitude__isnull=False,
        )
    )


def nearest_parcels(
    origin: Coordinates,
    *,
    county: str,
    within_miles: float,
    backed_by: QuerySet,
    exclude_account: str | None = None,
    limit: int | None = None,
) -> dict[str, float]:
    """Return the nearest parcels to ``origin``, as ``{account_number: miles}``.

    Ordered nearest-first, capped at ``limit``. Both counties' comparables
    searches are built on this; the haversine distance is computed in the
    database and the bounding box that makes it indexable is an implementation
    detail — callers say how far, not how to narrow.

    ``backed_by`` is a queryset yielding the account numbers a parcel must
    appear in to be considered: Harris passes properties (or buildings in a
    size range), Brazos passes accounts for the target tax year. It is
    **required, and applied before the cap**. ParcelGeometry holds every parcel
    in the source shapefile, so on real Harris data 24% of rows have no
    property behind them; capping first and filtering afterwards silently spent
    37.6% of one sampled search's candidate slots on parcels that were then
    discarded. Making the restriction a required argument is what stops that
    from being expressible.
    """
    # Resolved here rather than as a default argument: a default binds at
    # definition time, which would make MAX_NEARBY_PARCELS unpatchable and
    # quietly turn the cap regression tests into no-ops.
    if limit is None:
        limit = MAX_NEARBY_PARCELS

    latitude = float(origin.latitude)
    longitude = float(origin.longitude)

    latitude_span = within_miles / _MILES_PER_DEGREE_LATITUDE
    longitude_span = within_miles / (_MILES_PER_DEGREE_LATITUDE * cos(radians(latitude)))

    candidates = ParcelGeometry.objects.filter(
        county=county,
        latitude__gte=latitude - latitude_span,
        latitude__lte=latitude + latitude_span,
        longitude__gte=longitude - longitude_span,
        longitude__lte=longitude + longitude_span,
        latitude__isnull=False,
        longitude__isnull=False,
    ).filter(account_number__in=backed_by)

    if exclude_account is not None:
        candidates = candidates.exclude(account_number=exclude_account)

    origin_latitude = radians(latitude)
    origin_longitude = radians(longitude)

    # Great-circle distance in miles. Least/Greatest clamp the cosine into
    # [-1, 1]: floating-point drift can push it just outside, and ACos of that
    # is a database error rather than a rounding nuisance.
    distance = ExpressionWrapper(
        3959.0
        * ACos(
            Least(
                1.0,
                Greatest(
                    -1.0,
                    Cos(Value(origin_latitude))
                    * Cos(Radians(F("latitude")))
                    * Cos(Radians(F("longitude")) - Value(origin_longitude))
                    + Sin(Value(origin_latitude)) * Sin(Radians(F("latitude"))),
                ),
            )
        ),
        output_field=FloatField(),
    )

    rows = (
        candidates.annotate(distance=distance)
        .filter(distance__lte=within_miles)
        .order_by("distance")[:limit]
        .values_list("account_number", "distance")
    )
    return dict(rows)
