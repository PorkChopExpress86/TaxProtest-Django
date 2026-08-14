"""Load GIS parcel coordinates from a shapefile into PropertyRecord.

Moved here from ``counties/harris/etl.py`` so the ETL pipeline no longer
reaches across a package boundary for a function that is logically a
pipeline load step. The orchestrator calls this when processing the GIS
data source (see ``ETLOrchestrator._process_gis_source``).
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Iterable

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

    if not GEOPANDAS_AVAILABLE or gpd is None:
        raise ImportError(
            "geopandas is required to process GIS data. Install with: pip install geopandas pyogrio"
        )

    # Read shapefile
    assert gpd is not None  # for type checkers; guarded above
    gdf = gpd.read_file(shapefile_path)

    # Centroid first, then reproject to WGS84. A centroid is a planar
    # calculation, so it belongs in the shapefile's own projected CRS --
    # running it on lat/long degrees is what makes geopandas warn "Geometry is
    # in a geographic CRS". The positional difference is sub-metre on
    # parcel-sized polygons, but this order is the correct one and it
    # reprojects N points instead of N polygons.
    centroids = gdf.geometry.centroid
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        centroids = centroids.to_crs(epsg=4326)

    gdf["latitude"] = centroids.y
    gdf["longitude"] = centroids.x

    # Identify account number column (HCAD uses various names)
    account_col = None
    for col in gdf.columns:
        col_upper = col.upper()
        if col_upper in ["HCAD_NUM", "ACCT", "ACCOUNT", "ACCOUNT_NUM", "ACCT_NUM"]:
            account_col = col
            break

    if not account_col:
        raise ValueError(
            f"Could not find account number column in shapefile. Available columns: {list(gdf.columns)}"
        )

    # Identify parcel ID column
    parcel_col = None
    for col in gdf.columns:
        col_upper = col.upper()
        if col_upper in ["PARCEL_ID", "PARCELID", "PRCL_ID", "HCAD_NUM"]:
            parcel_col = col
            break

    updates_by_account: dict[str, tuple[float, float, str]] = {}
    total_updated = 0

    logger.info("Processing %s parcel records from %s", len(gdf), shapefile_path)

    for row in gdf.itertuples(index=False):
        account_num = str(getattr(row, account_col)).strip() if account_col else ""
        if not account_num:
            continue

        lat = getattr(row, "latitude", None)
        lon = getattr(row, "longitude", None)
        if lat is None or lon is None or _is_nan(lat) or _is_nan(lon):
            continue

        parcel_raw = getattr(row, parcel_col) if parcel_col else ""
        parcel_id = str(parcel_raw).strip() if parcel_raw is not None else ""
        updates_by_account[account_num] = (lat, lon, parcel_id)

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
