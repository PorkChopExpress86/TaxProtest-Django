"""Room counts from fixtures.txt when HCAD repeats a building's room type."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, TestCase

from counties.harris.etl_pipeline.fixtures_aggregator import (
    FixturesAggregator,
    update_building_room_counts,
)
from counties.harris.models import BuildingDetail, PropertyRecord

_HEADER = "acct\tbld_num\ttype\ttype_dscr\tunits\n"


def _write_fixtures(directory: str, rows: str) -> Path:
    path = Path(directory) / "fixtures.txt"
    path.write_text(_HEADER + rows, encoding="utf-8")
    return path


class ConflictingRoomCountTests(SimpleTestCase):
    def _aggregate(self, rows: str) -> FixturesAggregator:
        aggregator = FixturesAggregator()
        with TemporaryDirectory() as directory:
            aggregator.load_fixtures_file(_write_fixtures(directory, rows))
        return aggregator

    def test_a_conflicting_bedroom_count_is_unknown_and_other_counts_survive(self) -> None:
        aggregator = self._aggregate(
            "P1\t1\tRMB\tRoom:  Bedroom\t3.00\n"
            "P1\t1\tRMF\tRoom:  Full Bath\t2.00\n"
            "P1\t1\tRMB\tRoom:  Bedroom\t1.00\n"
            "P1\t1\tRMB\tRoom:  Bedroom\t3.00\n"
        )

        self.assertEqual(aggregator.get_bedroom_count("P1", 1), 0)
        self.assertEqual(aggregator.get_bathroom_count("P1", 1), 2.0)
        self.assertEqual(aggregator.fixture_fields[("P1", 1)], frozenset({"full_baths"}))

    def test_a_conflicting_full_or_half_bath_count_makes_the_bathroom_total_unknown(self) -> None:
        for conflicting, known in (("RMF", "half_baths"), ("RMH", "full_baths")):
            with self.subTest(conflicting=conflicting):
                aggregator = self._aggregate(
                    "P1\t1\tRMB\tRoom:  Bedroom\t3.00\n"
                    "P1\t1\tRMF\tRoom:  Full Bath\t2.00\n"
                    "P1\t1\tRMH\tRoom:  Half Bath\t1.00\n"
                    f"P1\t1\t{conflicting}\tRoom\t4.00\n"
                )

                self.assertEqual(aggregator.get_bedroom_count("P1", 1), 3)
                self.assertEqual(aggregator.get_bathroom_count("P1", 1), 0.0)
                self.assertEqual(
                    aggregator.fixture_fields[("P1", 1)], frozenset({"bedrooms", known})
                )

    def test_a_repeated_room_count_with_the_same_units_is_kept(self) -> None:
        aggregator = self._aggregate(
            "P1\t1\tRMB\tRoom:  Bedroom\t3.00\n" "P1\t1\tRMB\tRoom:  Bedroom\t3\n"
        )

        self.assertEqual(aggregator.get_bedroom_count("P1", 1), 3)


class ConflictingRoomCountRefreshTests(TestCase):
    def test_refresh_leaves_counts_it_cannot_know_unchanged(self) -> None:
        prop = PropertyRecord.objects.create(
            account_number="P1",
            address="1 MAIN ST",
            city="Houston",
            zipcode="77001",
            state_class="A1",
            is_residential=True,
        )
        building = BuildingDetail.objects.create(
            property=prop,
            account_number="P1",
            building_number=1,
            bedrooms=5,
            bathrooms=Decimal("3.5"),
            half_baths=1,
        )
        with TemporaryDirectory() as directory:
            update_building_room_counts(
                _write_fixtures(
                    directory,
                    "P1\t1\tRMB\tRoom:  Bedroom\t3.00\n"
                    "P1\t1\tRMB\tRoom:  Bedroom\t1.00\n"
                    "P1\t1\tRMF\tRoom:  Full Bath\t2.00\n"
                    "P1\t1\tRMF\tRoom:  Full Bath\t1.00\n"
                    "P1\t1\tRMH\tRoom:  Half Bath\t2.00\n",
                ),
                refresh_readiness=False,
            )

        building.refresh_from_db()
        self.assertEqual(
            (building.bedrooms, building.bathrooms, building.half_baths), (5, Decimal("3.5"), 2)
        )
