"""Tests for counties/common/geometry.py.

The facts these pin are the ones every caller previously had to re-derive:
county scoping, the difference between "no row" and "row with null
coordinates", and that the SQL form is an anti-join rather than NOT IN.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from counties.common.geometry import (
    Coordinates,
    accounts_with_coordinates,
    coordinates_exist,
    coordinates_for,
)
from counties.common.tax_models import ParcelGeometry
from counties.harris.models import PropertyRecord


def _geometry(account_number, *, county="harris", latitude="29.76", longitude="-95.37"):
    return ParcelGeometry.objects.create(
        account_number=account_number,
        county=county,
        latitude=Decimal(latitude) if latitude is not None else None,
        longitude=Decimal(longitude) if longitude is not None else None,
    )


class CoordinatesForTests(TestCase):
    def test_returns_coordinates_for_a_known_account(self) -> None:
        _geometry("ACCT1")

        location = coordinates_for("ACCT1", county="harris")

        self.assertEqual(location, Coordinates(Decimal("29.7600000"), Decimal("-95.3700000")))

    def test_unknown_account_is_none(self) -> None:
        self.assertIsNone(coordinates_for("NOPE", county="harris"))

    def test_row_with_null_coordinates_is_none(self) -> None:
        # A row exists, but the coordinates are not known -- to a caller that is
        # the same answer as no row at all, so it must not leak through as a
        # record with None fields.
        _geometry("ACCT1", latitude=None, longitude=None)

        self.assertIsNone(coordinates_for("ACCT1", county="harris"))

    def test_half_populated_row_is_none(self) -> None:
        _geometry("ACCT1", longitude=None)

        self.assertIsNone(coordinates_for("ACCT1", county="harris"))

    def test_county_scopes_the_lookup(self) -> None:
        _geometry("SHARED", county="brazos")

        self.assertIsNone(coordinates_for("SHARED", county="harris"))
        self.assertIsNotNone(coordinates_for("SHARED", county="brazos"))


class AccountsWithCoordinatesTests(TestCase):
    def test_returns_only_the_accounts_that_have_coordinates(self) -> None:
        _geometry("HAS1")
        _geometry("HAS2")
        _geometry("NULLED", latitude=None, longitude=None)

        found = accounts_with_coordinates(["HAS1", "HAS2", "NULLED", "ABSENT"], county="harris")

        self.assertEqual(found, {"HAS1", "HAS2"})

    def test_empty_input_returns_empty_without_querying(self) -> None:
        _geometry("HAS1")

        with self.assertNumQueries(0):
            self.assertEqual(accounts_with_coordinates([], county="harris"), set())

    def test_county_scopes_the_lookup(self) -> None:
        _geometry("SHARED", county="brazos")

        self.assertEqual(accounts_with_coordinates(["SHARED"], county="harris"), set())
        self.assertEqual(accounts_with_coordinates(["SHARED"], county="brazos"), {"SHARED"})


class CoordinatesExistTests(TestCase):
    def setUp(self) -> None:
        for account_number in ("WITH", "WITHOUT"):
            PropertyRecord.objects.create(
                address=f"{account_number} ST",
                city="Houston",
                zipcode="77001",
                account_number=account_number,
                state_class="A1",
                is_residential=True,
            )
        _geometry("WITH")
        _geometry("WITHOUT", latitude=None, longitude=None)

    def test_filters_to_properties_that_have_coordinates(self) -> None:
        found = PropertyRecord.objects.filter(coordinates_exist(county="harris"))

        self.assertEqual([p.account_number for p in found], ["WITH"])

    def test_negation_selects_the_properties_without_coordinates(self) -> None:
        found = PropertyRecord.objects.filter(~coordinates_exist(county="harris"))

        self.assertEqual([p.account_number for p in found], ["WITHOUT"])

    def test_negation_compiles_to_an_anti_join_not_a_not_in(self) -> None:
        """The whole reason this is Exists and not account_number__in.

        NOT IN cannot use a hash anti-join, and at production row counts that
        difference was over an hour versus under a second. Assert on the
        generated SQL so a well-meaning simplification back to __in gets caught.
        """
        sql = str(PropertyRecord.objects.filter(~coordinates_exist(county="harris")).query)

        self.assertIn("NOT EXISTS", sql.upper())
        self.assertNotIn("NOT IN", sql.upper())

    def test_annotation_form_works_for_streaming_callers(self) -> None:
        rows = PropertyRecord.objects.annotate(
            has_coords=coordinates_exist(county="harris")
        ).order_by("account_number")

        self.assertEqual(
            [(r.account_number, r.has_coords) for r in rows], [("WITH", True), ("WITHOUT", False)]
        )
