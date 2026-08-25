"""
Fixtures aggregator for bedroom and bathroom counts.

This module processes the HCAD fixtures.txt file to extract bedroom and bathroom
counts that are stored as separate fixture records.
"""

import logging
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)


class FixturesAggregator:
    """Aggregates fixture data for buildings."""

    # Fixture type codes for bedrooms and bathrooms
    BEDROOM_CODE = "RMB"  # Room: Bedroom
    FULL_BATH_CODE = "RMF"  # Room: Full Bath
    HALF_BATH_CODE = "RMH"  # Room: Half Bath

    def __init__(self):
        """Initialize the fixtures aggregator."""
        self.fixtures_cache: dict[tuple[str, int], dict[str, float]] = {}
        self.fixture_fields: dict[tuple[str, int], frozenset[str]] = {}
        self.total_fixture_records = 0
        self.room_records_found = 0

    def load_fixtures_file(self, file_path: Path) -> None:
        """
        Load and aggregate fixtures from fixtures.txt file.

        Args:
            file_path: Path to the fixtures.txt file

        The fixtures file has format:
        acct    bld_num    type    type_dscr    units
        1234567890123    1    RMB    Room: Bedroom    4.00
        1234567890123    1    RMF    Room: Full Bath    2.00
        1234567890123    1    RMH    Room: Half Bath    1.00
        """
        logger.info(f"Loading fixtures from {file_path}")
        self.fixtures_cache = {}
        self.fixture_fields = {}
        self.total_fixture_records = 0
        self.room_records_found = 0

        # Use defaultdict to accumulate fixture counts
        fixtures_data = defaultdict(lambda: {"bedrooms": 0.0, "full_baths": 0.0, "half_baths": 0.0})
        fixture_fields: defaultdict[tuple[str, int], set[str]] = defaultdict(set)

        line_count = 0
        processed_count = 0

        try:
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                header = next(f, "")
                field_positions = {
                    name.strip().lower(): index
                    for index, name in enumerate(header.rstrip("\n").split("\t"))
                }
                required_fields = ("acct", "bld_num", "type", "units")
                if any(field not in field_positions for field in required_fields):
                    logger.warning("Fixtures file has no usable room-count columns: %s", file_path)
                    return
                max_position = max(field_positions[field] for field in required_fields)

                for line in f:
                    line_count += 1

                    # Progress logging every 100k lines
                    if line_count % 100000 == 0:
                        logger.info(f"Processed {line_count:,} fixture lines...")

                    try:
                        parts = line.strip().split("\t")
                        if len(parts) <= max_position:
                            continue

                        account_num = parts[field_positions["acct"]].strip()
                        building_num = int(parts[field_positions["bld_num"]].strip())
                        fixture_type = parts[field_positions["type"]].strip()
                        units = float(parts[field_positions["units"]].strip())

                        # Only process bedroom/bathroom fixture types
                        if fixture_type not in (
                            self.BEDROOM_CODE,
                            self.FULL_BATH_CODE,
                            self.HALF_BATH_CODE,
                        ):
                            continue

                        key = (account_num, building_num)

                        if fixture_type == self.BEDROOM_CODE:
                            fixtures_data[key]["bedrooms"] = units
                            fixture_fields[key].add("bedrooms")
                        elif fixture_type == self.FULL_BATH_CODE:
                            fixtures_data[key]["full_baths"] = units
                            fixture_fields[key].add("full_baths")
                        elif fixture_type == self.HALF_BATH_CODE:
                            fixtures_data[key]["half_baths"] = units
                            fixture_fields[key].add("half_baths")

                        processed_count += 1

                    except (ValueError, IndexError) as e:
                        logger.debug(f"Error parsing fixture line {line_count}: {e}")
                        continue

        except FileNotFoundError:
            logger.warning(f"Fixtures file not found: {file_path}")
            return
        except Exception as e:
            logger.error(f"Error reading fixtures file: {e}")
            return

        # Convert defaultdict to regular dict and cache
        self.fixtures_cache = dict(fixtures_data)
        self.fixture_fields = {key: frozenset(fields) for key, fields in fixture_fields.items()}
        self.total_fixture_records = line_count
        self.room_records_found = processed_count

        logger.info(
            f"Loaded fixtures for {len(self.fixtures_cache):,} buildings "
            f"from {line_count:,} lines ({processed_count:,} bedroom/bathroom records)"
        )

    def get_fixtures(self, account_number: str, building_number: int) -> dict[str, float]:
        """
        Get aggregated fixtures for a specific building.

        Args:
            account_number: Property account number
            building_number: Building number

        Returns:
            Dictionary with bedrooms, full_baths, half_baths (0.0 if not found)
        """
        key = (account_number, building_number)
        return self.fixtures_cache.get(key, {"bedrooms": 0.0, "full_baths": 0.0, "half_baths": 0.0})

    def get_bedroom_count(self, account_number: str, building_number: int) -> int:
        """Get bedroom count for a building."""
        fixtures = self.get_fixtures(account_number, building_number)
        return int(fixtures["bedrooms"])

    def get_bathroom_count(self, account_number: str, building_number: int) -> float:
        """
        Get total bathroom count for a building.

        Total bathrooms = full_baths + (half_baths * 0.5)
        """
        fixtures = self.get_fixtures(account_number, building_number)
        full = fixtures["full_baths"]
        half = fixtures["half_baths"]
        return full + (half * 0.5)

    def clear_cache(self) -> None:
        """Clear the fixtures cache."""
        self.fixtures_cache.clear()
        logger.info("Fixtures cache cleared")

    def get_stats(self) -> dict[str, int]:
        """Get statistics about loaded fixtures."""
        if not self.fixtures_cache:
            return {"total_buildings": 0, "with_bedrooms": 0, "with_bathrooms": 0, "with_both": 0}

        with_bedrooms = sum(1 for f in self.fixtures_cache.values() if f["bedrooms"] > 0)
        with_full_baths = sum(1 for f in self.fixtures_cache.values() if f["full_baths"] > 0)
        with_half_baths = sum(1 for f in self.fixtures_cache.values() if f["half_baths"] > 0)
        with_bathrooms = sum(
            1 for f in self.fixtures_cache.values() if f["full_baths"] > 0 or f["half_baths"] > 0
        )
        with_both = sum(
            1
            for f in self.fixtures_cache.values()
            if f["bedrooms"] > 0 and (f["full_baths"] > 0 or f["half_baths"] > 0)
        )

        return {
            "total_buildings": len(self.fixtures_cache),
            "with_bedrooms": with_bedrooms,
            "with_full_baths": with_full_baths,
            "with_half_baths": with_half_baths,
            "with_bathrooms": with_bathrooms,
            "with_both": with_both,
        }


def update_building_room_counts(
    file_path: Path,
    *,
    chunk_size: int = 5000,
    refresh_readiness: bool = True,
) -> dict[str, int]:
    """Apply fixture-derived room counts to already-loaded building rows.

    The normal translated-row path supplies fixture counts while it creates
    BuildingDetail rows. This entry point preserves the recovery workflow for
    a fixtures-only refresh without reintroducing parsing into a command.
    """
    from django.db import transaction

    from counties.harris.models import BuildingDetail

    aggregator = FixturesAggregator()
    aggregator.load_fixtures_file(file_path)
    results = {
        "total_fixture_records": aggregator.total_fixture_records,
        "room_records_found": aggregator.room_records_found,
        "buildings_updated": 0,
        "buildings_not_found": 0,
    }
    if not aggregator.fixtures_cache:
        if refresh_readiness:
            from .readiness import refresh_property_readiness

            refresh_property_readiness()
        return results

    accounts = {account_number for account_number, _ in aggregator.fixtures_cache}
    buildings_by_key: dict[tuple[str, int], list[BuildingDetail]] = defaultdict(list)
    queryset = BuildingDetail.objects.filter(
        is_active=True,
        account_number__in=accounts,
    ).only("id", "account_number", "building_number", "bedrooms", "bathrooms", "half_baths")
    for building in queryset.iterator(chunk_size=chunk_size):
        buildings_by_key[(building.account_number, int(building.building_number or 0))].append(
            building
        )

    pending: list[BuildingDetail] = []

    def flush() -> None:
        if not pending:
            return
        with transaction.atomic():
            BuildingDetail.objects.bulk_update(
                pending,
                ["bedrooms", "bathrooms", "half_baths"],
                batch_size=chunk_size,
            )
        results["buildings_updated"] += len(pending)
        pending.clear()

    for key, fixture_counts in aggregator.fixtures_cache.items():
        buildings = buildings_by_key.get(key)
        if not buildings:
            results["buildings_not_found"] += 1
            continue

        fixture_fields = aggregator.fixture_fields[key]
        bedrooms = int(fixture_counts["bedrooms"])
        half_baths = int(fixture_counts["half_baths"])
        bathrooms = Decimal(str(fixture_counts["full_baths"])) + (Decimal("0.5") * half_baths)
        for building in buildings:
            changed = False
            if "bedrooms" in fixture_fields and building.bedrooms != bedrooms:
                building.bedrooms = bedrooms
                changed = True
            if {"full_baths", "half_baths"}.intersection(
                fixture_fields
            ) and building.bathrooms != bathrooms:
                building.bathrooms = bathrooms
                changed = True
            if "half_baths" in fixture_fields and building.half_baths != half_baths:
                building.half_baths = half_baths
                changed = True
            if not changed:
                continue
            pending.append(building)
            if len(pending) >= chunk_size:
                flush()

    flush()

    if refresh_readiness:
        from .readiness import refresh_property_readiness

        refresh_property_readiness()
    return results
