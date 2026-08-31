"""Internal interpretation of coordinate evidence from a BCAD GIS frame."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from django.core.management.base import CommandError

Coordinate = tuple[Decimal, Decimal]


def normalize_prop_id(value: Any) -> str | None:
    """Return BCAD's canonical 12-digit property identifier, if usable."""
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return str(normalized).zfill(12)


@dataclass(frozen=True, slots=True)
class GisCoordinateEvidence:
    """Normalized usable coordinates and source-quality metrics."""

    coordinates: Mapping[str, Coordinate]
    source_records: int
    usable_coordinate_records: int
    distinct_source_ids: int
    duplicate_source_ids: int
    invalid_coordinate_records: int


def interpret_gis_coordinates(frame: Any) -> GisCoordinateEvidence:
    """Interpret one already-read BCAD parcel frame without publication policy."""
    if "PROP_ID" not in frame:
        raise CommandError("BCAD GIS source has no PROP_ID column.")
    if frame.crs is None:
        raise CommandError("BCAD GIS source has no coordinate reference system.")

    centroids = frame.geometry.centroid
    if frame.crs.to_epsg() != 4326:
        centroids = centroids.to_crs(epsg=4326)

    normalized_ids: list[str] = []
    coordinates: dict[str, Coordinate] = {}
    invalid_coordinate_records = 0
    for raw_prop_id, point in zip(frame["PROP_ID"], centroids, strict=True):
        prop_id = normalize_prop_id(raw_prop_id)
        if prop_id is None:
            continue
        normalized_ids.append(prop_id)
        if point is None or point.is_empty:
            invalid_coordinate_records += 1
            continue
        try:
            latitude = Decimal(str(point.y))
            longitude = Decimal(str(point.x))
        except (ArithmeticError, ValueError):
            invalid_coordinate_records += 1
            continue
        if not latitude.is_finite() or not longitude.is_finite():
            invalid_coordinate_records += 1
            continue
        coordinates[prop_id] = (latitude, longitude)

    counts = Counter(normalized_ids)
    return GisCoordinateEvidence(
        coordinates=MappingProxyType(coordinates),
        source_records=len(frame),
        usable_coordinate_records=len(coordinates),
        distinct_source_ids=len(counts),
        duplicate_source_ids=sum(1 for count in counts.values() if count > 1),
        invalid_coordinate_records=invalid_coordinate_records,
    )
