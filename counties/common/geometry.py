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

from django.db.models import Exists, OuterRef

from .tax_models import ParcelGeometry


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
