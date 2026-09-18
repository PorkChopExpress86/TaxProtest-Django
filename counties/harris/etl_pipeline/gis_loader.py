"""Load GIS parcel coordinates from a shapefile into PropertyRecord.

Moved here from ``counties/harris/etl.py`` so the ETL pipeline no longer
reaches across a package boundary for a function that is logically a
pipeline load step. The orchestrator calls this when processing the GIS
data source (see the private execution behind ``run_harris_import``).
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.db import connection, transaction

from counties.harris.models import PropertyRecord

logger = logging.getLogger(__name__)

try:
    import geopandas as gpd  # type: ignore

    GEOPANDAS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    gpd = None  # type: ignore
    GEOPANDAS_AVAILABLE = False


def _is_nan(value: object) -> bool:
    try:
        return math.isnan(value)  # type: ignore[arg-type]
    except TypeError:
        return False


def select_preferred_gis_shapefile(search_roots: Sequence[Path]) -> Path | None:
    """Choose the best parcel layer from one or more extracted GIS roots."""
    shapefiles: list[Path] = []
    seen: set[str] = set()

    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.shp"):
            normalized = str(path).replace("\\", "/")
            if normalized in seen:
                continue
            seen.add(normalized)
            shapefiles.append(path)

    if not shapefiles:
        geodatabases: list[Path] = []
        for root in search_roots:
            if not root.exists():
                continue
            for path in root.rglob("*.gdb"):
                if path.is_dir():
                    normalized = str(path).replace("\\", "/")
                    if normalized not in seen:
                        seen.add(normalized)
                        geodatabases.append(path)
        if geodatabases:
            return min(
                geodatabases,
                key=lambda path: (0 if "parcels.gdb" in path.name.lower() else 1, len(path.parts)),
            )
        return None

    def priority(path: Path) -> tuple[int, int, int]:
        normalized = str(path).replace("\\", "/").lower()
        name = path.name.lower()
        return (
            2 if name == "parcelscity.shp" else 1 if "parcelscity" in name else 0,
            1 if "/gis/pdata/" in normalized else 0,
            -len(path.parts),
        )

    return max(shapefiles, key=priority)


@dataclass(frozen=True)
class GisParcelInspection:
    coordinates: dict[str, tuple[float, float, str]]
    invalid: int
    skipped: int
    source_years: tuple[int, ...]


def inspect_gis_parcels(
    shapefile_path: str, *, expected_source_year: int | None = None
) -> GisParcelInspection:
    """Inspect canonical parcel identities, coordinates, and recorded source years."""
    if not GEOPANDAS_AVAILABLE or gpd is None:
        raise ImportError(
            "geopandas is required to process GIS data. Install with: pip install geopandas pyogrio"
        )

    gdf = gpd.read_file(shapefile_path)
    if gdf.crs is None:
        raise ValueError("GIS coordinate reference system is missing")
    centroids = gdf.geometry.centroid
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        centroids = centroids.to_crs(epsg=4326)

    gdf["latitude"] = centroids.y
    gdf["longitude"] = centroids.x

    account_col = next(
        (
            column
            for column in gdf.columns
            if column.upper() in {"HCAD_NUM", "ACCT", "ACCOUNT", "ACCOUNT_NUM", "ACCT_NUM"}
        ),
        None,
    )
    if account_col is None:
        raise ValueError(
            f"Could not find account number column in shapefile. Available columns: {list(gdf.columns)}"
        )

    parcel_col = next(
        (
            column
            for column in gdf.columns
            if column.upper() in {"PARCEL_ID", "PARCELID", "PRCL_ID", "HCAD_NUM"}
        ),
        None,
    )

    updates_by_account: dict[str, tuple[float, float, str]] = {}
    invalid = skipped = 0
    source_years: set[int] = set()
    year_column = next(
        (
            column
            for column in gdf.columns
            if column.lower() in ("tax_year", "taxyear", "data_year", "year")
        ),
        None,
    )
    for row in gdf.itertuples(index=False):
        if year_column is not None:
            raw_year = getattr(row, year_column)
            if raw_year is not None and str(raw_year).strip().lower() not in (
                "",
                "nan",
                "none",
                "<na>",
            ):
                year = Decimal(str(raw_year))
                if not year.is_finite() or year != year.to_integral_value():
                    raise ValueError("Invalid GIS source year")
                source_years.add(int(year))
                if expected_source_year is not None and year != expected_source_year:
                    raise ValueError(
                        f"GIS source year {year} differs from requested {expected_source_year}"
                    )
        account_num = str(getattr(row, account_col)).strip()
        if not account_num or account_num.lower() in ("nan", "none", "<na>"):
            invalid += 1
            continue
        if len(account_num) > 20:
            raise ValueError("GIS canonical account identity exceeds 20 characters")

        lat = getattr(row, "latitude", None)
        lon = getattr(row, "longitude", None)
        if (
            lat is None
            or lon is None
            or not math.isfinite(lat)
            or not math.isfinite(lon)
            or not (-90 <= lat <= 90 and -180 <= lon <= 180)
        ):
            invalid += 1
            continue

        parcel_raw = getattr(row, parcel_col) if parcel_col else ""
        parcel_id = str(parcel_raw).strip() if parcel_raw is not None else ""
        coordinates = (lat, lon, parcel_id)
        if account_num in updates_by_account and updates_by_account[account_num] != coordinates:
            raise ValueError(f"Conflicting canonical GIS account identity: {account_num}")
        if account_num in updates_by_account:
            skipped += 1
        updates_by_account[account_num] = coordinates

    return GisParcelInspection(updates_by_account, invalid, skipped, tuple(sorted(source_years)))


def translate_gis_parcels(shapefile_path: str) -> dict[str, tuple[float, float, str]]:
    """Translate without writing; preserve the existing coordinates-only contract."""
    return inspect_gis_parcels(shapefile_path).coordinates


def load_gis_parcels(
    shapefile_path: str,
    chunk_size: int = 5000,
    refresh_readiness: bool = True,
) -> int:
    """Load GIS parcel data from shapefile and update PropertyRecord with lat/long.

    Expected shapefile columns:
    - HCAD_NUM or ACCT or similar (account number)
    - Geometry (point or polygon centroid for lat/long)

    Returns number of records updated.
    """
    from .readiness import refresh_property_readiness

    updates_by_account = translate_gis_parcels(shapefile_path)
    total_updated = 0
    logger.info("Processing %s parcel records from %s", len(updates_by_account), shapefile_path)
    if not updates_by_account:
        logger.info("No valid GIS rows found in %s", shapefile_path)
        return 0

    if connection.vendor == "postgresql":
        # Set-based update: stage the parsed coordinates into a TEMP table via COPY,
        # then apply them with a single UPDATE ... FROM join on account_number.
        # This replaces a per-chunk bulk_update loop over ~1.2M rows (which emitted a
        # giant CASE statement per batch and maintained the lat/long/parcel_id indexes
        # row-by-row); the join-based update is roughly an order of magnitude faster.
        logger.info("Staging %s GIS updates for set-based apply", len(updates_by_account))

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("""
                CREATE TEMP TABLE _gis_staging (
                    account_number varchar(20) PRIMARY KEY,
                    latitude numeric,
                    longitude numeric,
                    parcel_id varchar(50)
                ) ON COMMIT DROP
                """)

            def _copy_rows() -> Iterable[str]:
                for account_num, (lat, lon, parcel_id) in updates_by_account.items():
                    # Tab-delimited COPY; account numbers/parcel ids never contain tabs.
                    yield f"{account_num}\t{lat}\t{lon}\t{parcel_id}\n"

            copy_buffer = io.StringIO("".join(_copy_rows()))
            cursor.copy_expert(
                "COPY _gis_staging (account_number, latitude, longitude, parcel_id) "
                "FROM STDIN WITH (FORMAT text)",
                copy_buffer,
            )

            # Only overwrite parcel_id when the staged value is non-empty, preserving the
            # prior behavior where a blank shapefile parcel id did not clobber existing data.
            cursor.execute("""
                UPDATE data_propertyrecord AS p
                SET latitude = s.latitude,
                    longitude = s.longitude,
                    parcel_id = CASE WHEN s.parcel_id <> '' THEN s.parcel_id ELSE p.parcel_id END
                FROM _gis_staging AS s
                WHERE p.account_number = s.account_number
                  AND p.is_residential
                """)
            total_updated = cursor.rowcount
    else:
        batch: list[PropertyRecord] = []
        properties = PropertyRecord.objects.filter(
            account_number__in=updates_by_account.keys(),
            is_residential=True,
        ).only("id", "account_number", "latitude", "longitude", "parcel_id")

        with transaction.atomic():
            for prop in properties.iterator(chunk_size=chunk_size):
                update = updates_by_account.get(prop.account_number)
                if not update:
                    continue
                lat, lon, parcel_id = update

                prop.latitude = lat
                prop.longitude = lon
                if parcel_id:
                    prop.parcel_id = parcel_id
                batch.append(prop)

                if len(batch) >= chunk_size:
                    PropertyRecord.objects.bulk_update(
                        batch,
                        ["latitude", "longitude", "parcel_id"],
                        batch_size=chunk_size,
                    )
                    total_updated += len(batch)
                    logger.info("Updated %s properties with GIS data...", total_updated)
                    batch.clear()

            if batch:
                PropertyRecord.objects.bulk_update(
                    batch,
                    ["latitude", "longitude", "parcel_id"],
                    batch_size=chunk_size,
                )
                total_updated += len(batch)

    logger.info("Completed: Updated %s properties with GIS coordinates", total_updated)
    if refresh_readiness:
        refresh_property_readiness()
    return total_updated
