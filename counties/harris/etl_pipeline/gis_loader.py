"""Load GIS parcel coordinates from a shapefile into ParcelGeometry.

The single home for this loader. It moved out of ``counties/harris/etl.py``
so the pipeline no longer reaches across a package boundary for what is
logically a load step; the ``load_gis_data`` management command imports it
from here too, and the orchestrator calls it when processing the GIS data
source (see ``ETLOrchestrator._process_gis_source``).
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Iterable

from django.db import connection, transaction

from .fast_loader import _copy_field

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
    """Load GIS parcel data from shapefile into the ParcelGeometry table.

    This is a bulk INSERT (upsert) into ``data_parcelgeometry``, not an
    UPDATE of PropertyRecord — so it can run at any point in the ETL
    pipeline, independent of whether properties have been loaded yet.

    Expected shapefile columns:
    - HCAD_NUM or ACCT or similar (account number)
    - Geometry (point or polygon centroid for lat/long)

    Returns number of records upserted.
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

    updates_by_account: dict[str, tuple[float, float]] = {}

    logger.info("Processing %s parcel records from %s", len(gdf), shapefile_path)

    for row in gdf.itertuples(index=False):
        account_num = str(getattr(row, account_col)).strip() if account_col else ""
        if not account_num:
            continue

        lat = getattr(row, "latitude", None)
        lon = getattr(row, "longitude", None)
        if lat is None or lon is None or _is_nan(lat) or _is_nan(lon):
            continue

        updates_by_account[account_num] = (lat, lon)

    if not updates_by_account:
        logger.info("No valid GIS rows found in %s", shapefile_path)
        return 0

    total_upserted = 0

    if connection.vendor == "postgresql":
        # Upsert into ParcelGeometry via COPY into a temp staging table,
        # then INSERT ... ON CONFLICT to handle re-runs.
        logger.info("Staging %s GIS rows for upsert into ParcelGeometry", len(updates_by_account))

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("""
                CREATE TEMP TABLE _gis_staging (
                    account_number varchar(20) PRIMARY KEY,
                    latitude numeric,
                    longitude numeric
                ) ON COMMIT DROP
                """)

            # Escape before joining: COPY text format reads a backslash as an
            # escape and a tab as a column break, so an account number carrying
            # either would shift every following column on that row. HCAD's ids
            # are numeric today, but the shapefile is third-party input and this
            # costs nothing.
            def _copy_rows() -> Iterable[str]:
                for account_num, (lat, lon) in updates_by_account.items():
                    yield f"{_copy_field(account_num)}\t{lat}\t{lon}\n"

            copy_buffer = io.StringIO("".join(_copy_rows()))
            cursor.copy_expert(
                "COPY _gis_staging (account_number, latitude, longitude) "
                "FROM STDIN WITH (FORMAT text)",
                copy_buffer,
            )

            cursor.execute("""
                INSERT INTO data_parcelgeometry
                    (account_number, county, latitude, longitude, created_at, updated_at)
                SELECT s.account_number, 'harris', s.latitude, s.longitude, NOW(), NOW()
                FROM _gis_staging s
                ON CONFLICT (account_number, county) DO UPDATE
                SET latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    updated_at = NOW()
            """)
            total_upserted = cursor.rowcount
    else:
        from counties.common.tax_models import ParcelGeometry

        batch: list[ParcelGeometry] = []
        with transaction.atomic():
            for account_num, (lat, lon) in updates_by_account.items():
                geom = ParcelGeometry(
                    account_number=account_num,
                    county="harris",
                    latitude=lat,
                    longitude=lon,
                )
                batch.append(geom)

                if len(batch) >= chunk_size:
                    ParcelGeometry.objects.bulk_create(
                        batch,
                        update_conflicts=True,
                        update_fields=["latitude", "longitude", "updated_at"],
                        unique_fields=["account_number", "county"],
                        batch_size=chunk_size,
                    )
                    total_upserted += len(batch)
                    logger.info("Upserted %s parcel geometries...", total_upserted)
                    batch.clear()

            if batch:
                ParcelGeometry.objects.bulk_create(
                    batch,
                    update_conflicts=True,
                    update_fields=["latitude", "longitude", "updated_at"],
                    unique_fields=["account_number", "county"],
                    batch_size=chunk_size,
                )
                total_upserted += len(batch)

    logger.info("Completed: Upserted %s parcel geometries", total_upserted)
    if refresh_readiness:
        refresh_property_readiness()
    return total_upserted
