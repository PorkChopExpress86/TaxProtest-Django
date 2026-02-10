import csv
import os
import logging
from typing import Iterable, Dict, Optional, List

from django.db import transaction, connection

from .models import PropertyRecord

logger = logging.getLogger(__name__)

# Increase CSV field size limit to handle large HCAD fields
csv.field_size_limit(10485760)  # 10MB limit


try:
    import geopandas as gpd  # type: ignore
    GEOPANDAS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    gpd = None  # type: ignore
    GEOPANDAS_AVAILABLE = False


def sniff_delimiter(sample: str) -> str:
    """Try to detect delimiter among comma, tab, and pipe."""
    candidates = ["\t", "|", ","]
    counts = {d: sample.count(d) for d in candidates}
    # choose the delimiter with the max count (prefer tab/pipe over comma if tied)
    return max(counts, key=lambda d: (counts[d], 1 if d in ("\t", "|") else 0))


def open_reader(filepath: str) -> csv.DictReader:
    """Open a large text file and return a DictReader with detected delimiter.

    Handles UTF-8 with fallback to latin-1 if needed.
    """
    # Read a small sample to sniff
    with open(filepath, "rb") as f:
        sample_bytes = f.read(4096)
    try:
        sample = sample_bytes.decode("utf-8", errors="ignore")
        encoding = "utf-8"
    except Exception:
        sample = sample_bytes.decode("latin-1", errors="ignore")
        encoding = "latin-1"

    delimiter = sniff_delimiter(sample)

    # We need to re-open as text for DictReader
    f = open(filepath, "r", encoding=encoding, errors="ignore", newline="")
    # CSV may or may not have header; HCAD files generally include headers.
    return csv.DictReader(f, delimiter=delimiter)


def parse_currency(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    # remove $ and commas
    for ch in ["$", ","]:
        v = v.replace(ch, "")
    try:
        return float(v)
    except Exception:
        return None


def iter_property_rows(reader: csv.DictReader) -> Iterable[Dict]:
    """Yield normalized property rows from a Real Account file.

    This expects columns typically present in real_acct.txt such as:
      - SITE_ADDR_NUM, SITE_ADDR_STREET (or SITE_ADDR), ZIP
      - OWNER_NAME (or similar)
      - APPR_BLDG_VAL, APPR_LAND_VAL, APPR_EXFEAT_VAL, TOT_APPR_VAL, MKT_VAL
    We fallback to reasonable alternatives; unknown columns are ignored.
    """
    # Normalize field names to lowercase for matching
    lower_fieldnames = [fn.lower() for fn in (reader.fieldnames or [])]
    fieldmap = {fn.lower(): fn for fn in (reader.fieldnames or [])}

    def get(row: Dict, *names: str) -> str:
        for n in names:
            key = fieldmap.get(n.lower())
            if key and key in row:
                return (row.get(key) or "").strip()
        return ""

    for row in reader:
        # Address components (HCAD specific: str_num/str/str_sfx; or site_addr_1 as full)
        addr_num = get(
            row, "str_num", "site_addr_num", "situs_addr_num", "address_number"
        )
        addr_num_sfx = get(row, "str_num_sfx")
        street_name = get(row, "str", "site_addr_street", "situs_street", "street_name")
        street_pfx = get(row, "str_pfx")
        street_sfx = get(row, "str_sfx")
        street_sfx_dir = get(row, "str_sfx_dir")
        site_addr_1 = get(row, "site_addr_1", "site_addr")
        site_city = get(row, "site_addr_2", "situs_city", "city")
        raw_zip = get(row, "site_addr_3", "zip", "zip_code", "zipcode")
        zipcode = raw_zip[:5] if raw_zip and len(raw_zip) >= 5 else raw_zip

        # Market or total value
        value = (
            parse_currency(get(row, "tot_appr_val"))
            or parse_currency(get(row, "mkt_val"))
            or parse_currency(get(row, "appr_bldg_val"))
        )
        assessed_value = parse_currency(
            get(row, "assessed_val", "assessed_val", "tot_appr_val")
        )
        building_area = parse_currency(get(row, "bld_ar", "bldg_ar", "bld_area"))
        land_area = parse_currency(get(row, "land_ar", "land_area"))

        account_number = get(row, "acct", "account", "account_number")
        owner_name = get(row, "mailto", "owner_name", "owner")

        # Build address: prefer explicit components; fallback to site_addr_1
        number = "".join([p for p in [addr_num, addr_num_sfx] if p])
        street_parts = [street_pfx, street_name, street_sfx, street_sfx_dir]
        street = " ".join([p for p in street_parts if p])
        built_address = " ".join([p for p in [number, street] if p]).strip()
        address = built_address or site_addr_1

        yield {
            "address": address,
            "city": site_city,
            "zipcode": zipcode,
            "value": value,
            "assessed_value": assessed_value,
            "building_area": building_area,
            "land_area": land_area,
            "account_number": account_number,
            "owner_name": owner_name,
            "street_number": number,
            "street_name": street_name,
        }


def bulk_load_properties(
    filepath: str, chunk_size: int = 5000, limit: Optional[int] = None, truncate: bool = True
) -> int:
    """Load a Real Account file into PropertyRecord table.

    Args:
        filepath: Path to the real_acct.txt file
        chunk_size: Number of records to batch before bulk insert
        limit: Optional limit on number of rows to insert (for testing)
        truncate: If True, truncate the table before importing (default: True)
                  This ensures clean imports without duplicates on re-runs.

    Returns number of rows inserted.
    """
    reader = open_reader(filepath)
    buf: List[PropertyRecord] = []
    total = 0
    skipped_duplicates = 0
    
    # Truncate table for clean import if requested
    if truncate:
        logger.info("Truncating PropertyRecord table for clean import...")
        with connection.cursor() as cursor:
            cursor.execute('TRUNCATE TABLE "data_propertyrecord" RESTART IDENTITY CASCADE')
        logger.info("PropertyRecord table truncated successfully.")
        existing_accounts: set = set()
    else:
        # Pre-fetch existing account numbers to prevent duplicates
        # This is memory efficient enough for ~2M records (approx 30-50MB RAM)
        existing_accounts = set(PropertyRecord.objects.values_list('account_number', flat=True))
        logger.info(f"Loaded {len(existing_accounts)} existing accounts for deduplication.")

    with transaction.atomic():
        for idx, data in enumerate(iter_property_rows(reader), start=1):
            # simple validation: require either address or zipcode
            if not (data.get("address") or data.get("zipcode")):
                continue
                
            acct = data.get("account_number", "")[:20]
            
            # Skip if account already exists
            if acct in existing_accounts:
                skipped_duplicates += 1
                continue
                
            # Add to local set to prevent duplicates within the same file/batch
            existing_accounts.add(acct)
            
            buf.append(
                PropertyRecord(
                    address=data.get("address", "")[:255],
                    city=data.get("city", "")[:100],
                    zipcode=data.get("zipcode", "")[:20],
                    value=data.get("value"),
                    assessed_value=data.get("assessed_value"),
                    building_area=data.get("building_area"),
                    land_area=data.get("land_area"),
                    account_number=acct,
                    owner_name=data.get("owner_name", "")[:255],
                    street_number=data.get("street_number", "")[:16],
                    street_name=data.get("street_name", "")[:128],
                    source_url="hcad:real_acct",
                )
            )
            if len(buf) >= chunk_size:
                PropertyRecord.objects.bulk_create(buf, ignore_conflicts=True)
                total += len(buf)
                logger.info(f"Imported {total} records...")
                buf.clear()
            if limit and total >= limit:
                break
        if buf:
            PropertyRecord.objects.bulk_create(buf, ignore_conflicts=True)
            total += len(buf)
            
    if skipped_duplicates > 0:
        logger.info(f"Skipped {skipped_duplicates} duplicate records.")
        
    return total


def load_gis_parcels(shapefile_path: str, chunk_size: int = 5000) -> int:
    """Load GIS parcel data from shapefile and update PropertyRecord with lat/long.

    Expected shapefile columns:
    - HCAD_NUM or ACCT or similar (account number)
    - Geometry (point or polygon centroid for lat/long)

    Returns number of records updated.
    """
    if not GEOPANDAS_AVAILABLE or gpd is None:
        raise ImportError(
            "geopandas is required to process GIS data. Install with: pip install geopandas pyogrio"
        )

    # Read shapefile
    assert gpd is not None  # for type checkers; guarded above
    gdf = gpd.read_file(shapefile_path)

    # Ensure CRS is WGS84 (lat/long) for consistent coordinates
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    # Calculate centroids for lat/long
    gdf["centroid"] = gdf.geometry.centroid
    gdf["latitude"] = gdf["centroid"].y
    gdf["longitude"] = gdf["centroid"].x

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

    total_updated = 0
    batch = []

    logger.info(f"Processing %s parcel records from %s", len(gdf), shapefile_path)

    with transaction.atomic():
        for idx, row in gdf.iterrows():
            account_num = str(row[account_col]).strip() if account_col else None
            parcel_id = str(row[parcel_col]).strip() if parcel_col else None
            lat = row["latitude"]
            lon = row["longitude"]

            # Skip if account number is missing or coordinates are invalid
            if not account_num:
                continue

            # Check for NaN or invalid coordinates
            import math

            if lat is None or lon is None or math.isnan(lat) or math.isnan(lon):
                continue

            # Find matching property records by account number
            props = PropertyRecord.objects.filter(account_number=account_num)

            for prop in props:
                prop.latitude = lat
                prop.longitude = lon
                if parcel_id:
                    prop.parcel_id = parcel_id
                batch.append(prop)

                if len(batch) >= chunk_size:
                    PropertyRecord.objects.bulk_update(
                        batch, ["latitude", "longitude", "parcel_id"]
                    )
                    total_updated += len(batch)
                    logger.info("Updated %s properties with GIS data...", total_updated)
                    batch.clear()

        # Update remaining batch
        if batch:
            PropertyRecord.objects.bulk_update(
                batch, ["latitude", "longitude", "parcel_id"]
            )
            total_updated += len(batch)

    logger.info("Completed: Updated %s properties with GIS coordinates", total_updated)
    return total_updated


def load_building_details(
    filepath: str, chunk_size: int = 5000, import_batch_id: Optional[str] = None
) -> dict:
    """Load residential building details from building_res.txt.

    Expected columns: acct, bld_num, imprv_type, building_style_code, bldg_class,
    qual_cd, cndtn_cd, date_erected, yr_remodel, eff_yr, heat_ar, base_ar,
    gross_ar, foundation, exterior_wall, roof_cover, roof_typ, sty (stories),
    bed_rm, full_bath, half_bath, fireplace, etc.

    Args:
        filepath: Path to the building_res.txt file
        chunk_size: Number of records to batch before bulk insert
        import_batch_id: Optional batch identifier for tracking imports

    Returns:
        Dictionary with import statistics:
        {
            'imported': int,      # Successfully imported records
            'invalid': int,       # Records with invalid account numbers
            'skipped': int,       # Records skipped (no account)
        }
    """
    from .models import BuildingDetail, PropertyRecord
    from django.utils import timezone

    reader = open_reader(filepath)
    buf = []
    results = {
        "imported": 0,
        "invalid": 0,
        "skipped": 0,
    }

    # Generate batch ID if not provided
    if not import_batch_id:
        import_batch_id = timezone.now().strftime("%Y%m%d_%H%M%S")

    import_date = timezone.now()

    # Cache valid account numbers for validation
    valid_accounts = set(
        PropertyRecord.objects.values_list("account_number", flat=True)
    )
    logger.info("Loaded %s valid account numbers for validation", len(valid_accounts))

    logger.info("Loading building details from %s", filepath)
    logger.info("Import batch ID: %s", import_batch_id)

    # Truncate BuildingDetail table for clean import (faster than DELETE and resets sequences)
    logger.info("Truncating BuildingDetail table...")
    with connection.cursor() as cursor:
        cursor.execute('TRUNCATE TABLE "data_buildingdetail" RESTART IDENTITY CASCADE')
    logger.info("BuildingDetail table truncated successfully")

    with transaction.atomic():
        for idx, row in enumerate(reader, start=1):
            acct = (row.get("acct") or "").strip()
            if not acct:
                results["skipped"] += 1
                continue

            # Validate account number
            if acct not in valid_accounts:
                results["invalid"] += 1
                continue

            # Try to find the associated property
            try:
                prop = PropertyRecord.objects.filter(account_number=acct).first()
            except Exception:
                prop = None
                results["invalid"] += 1
                continue

            def get_int(field):
                val = (row.get(field) or "").strip()
                if val:
                    try:
                        return int(float(val))
                    except:
                        pass
                return None

            def get_decimal(field):
                val = (row.get(field) or "").strip()
                if val:
                    try:
                        return float(val)
                    except:
                        pass
                return None

            def get_str(field, maxlen=10):
                return (row.get(field) or "").strip()[:maxlen]

            building = BuildingDetail(
                property=prop,
                account_number=acct,
                building_number=get_int("bld_num"),
                building_type=get_str("imprv_type"),
                building_style=get_str("building_style_code"),
                building_class=get_str("bldg_class"),
                quality_code=get_str("qa_cd"),
                condition_code=get_str("cndtn_cd"),
                year_built=get_int("date_erected"),
                year_remodeled=get_int("yr_remodel"),
                effective_year=get_int("eff_yr"),
                heat_area=get_decimal("heat_ar"),
                base_area=get_decimal("base_ar"),
                gross_area=get_decimal("gross_ar"),
                stories=get_decimal("sty"),
                foundation_type=get_str("foundation"),
                exterior_wall=get_str("exterior_wall"),
                roof_cover=get_str("roof_cover"),
                roof_type=get_str("roof_typ"),
                # Bedrooms/Bathrooms not in building_res.txt; loaded later
                bedrooms=None,
                bathrooms=None,
                half_baths=None,
                fireplaces=None,
                # Import metadata
                is_active=True,
                import_date=import_date,
                import_batch_id=import_batch_id,
            )

            buf.append(building)

            if len(buf) >= chunk_size:
                BuildingDetail.objects.bulk_create(buf, ignore_conflicts=True)
                results["imported"] += len(buf)
                logger.info(
                    "Loaded %s building records (invalid: %s, skipped: %s)...",
                    results["imported"],
                    results["invalid"],
                    results["skipped"],
                )
                buf.clear()

        if buf:
            BuildingDetail.objects.bulk_create(buf, ignore_conflicts=True)
            results["imported"] += len(buf)

    logger.info("Completed: Loaded %s building detail records", results["imported"])
    logger.info(
        "Invalid account numbers: %s, Skipped: %s",
        results["invalid"],
        results["skipped"],
    )
    return results


def load_extra_features(
    filepath: str, 
    chunk_size: int = 5000, 
    import_batch_id: Optional[str] = None,
    truncate: bool = True
) -> dict:
    """Load extra features from extra_features_detail files.

    Expected columns in detail files: 
    acct, cd, dscr, grade, cond_cd, bld_num, length, width, units, unit_price, etc.

    Args:
        filepath: Path to the extra_features file
        chunk_size: Number of records to batch before bulk insert
        import_batch_id: Optional batch identifier for tracking imports
        truncate: Whether to truncate the table before loading (default: True)

    Returns:
        Dictionary with import statistics
    """
    from .models import ExtraFeature, PropertyRecord
    from django.utils import timezone

    reader = open_reader(filepath)
    buf = []
    results = {
        "imported": 0,
        "invalid": 0,
        "skipped": 0,
    }

    # Generate batch ID if not provided
    if not import_batch_id:
        import_batch_id = timezone.now().strftime("%Y%m%d_%H%M%S")

    import_date = timezone.now()

    # Cache valid account numbers for validation
    if truncate:
         logger.info("Preparing to load extra features...")

    valid_accounts = set(
        PropertyRecord.objects.values_list("account_number", flat=True)
    )
    
    logger.info("Loading extra features from %s", filepath)

    if truncate:
        logger.info("Truncating ExtraFeature table...")
        with connection.cursor() as cursor:
            cursor.execute('TRUNCATE TABLE "data_extrafeature" RESTART IDENTITY CASCADE')
        logger.info("ExtraFeature table truncated successfully")
    else:
        logger.info("Appending to ExtraFeature table (no truncate)...")

    with transaction.atomic():
        for idx, row in enumerate(reader, start=1):
            acct = (row.get("acct") or "").strip()
            if not acct:
                results["skipped"] += 1
                continue

            # Validate account number
            if acct not in valid_accounts:
                results["invalid"] += 1
                continue

            # Check prop existence
            try:
                prop = PropertyRecord.objects.filter(account_number=acct).first()
            except Exception:
                prop = None
                results["invalid"] += 1
                continue
            
            if not prop:
                 results["invalid"] += 1
                 continue

            def get_int(field):
                val = (row.get(field) or "").strip()
                if val:
                    try:
                        return int(float(val))
                    except:
                        pass
                return None

            def get_decimal(field):
                val = (row.get(field) or "").strip()
                if val:
                    try:
                        return float(val)
                    except:
                        pass
                return None

            def get_str(field, maxlen=10):
                return (row.get(field) or "").strip()[:maxlen]

            # Mapping for extra_features_detail*.txt
            feature = ExtraFeature(
                property=prop,
                account_number=acct,
                feature_number=get_int("bld_num"), 
                feature_code=get_str("cd", maxlen=10),
                feature_description=(row.get("dscr") or "").strip()[:255],
                quantity=get_decimal("units"),
                area=None, 
                length=get_decimal("length"),
                width=get_decimal("width"),
                quality_code=get_str("grade", maxlen=10),
                condition_code=get_str("cond_cd", maxlen=10),
                year_built=get_int("act_yr"),
                value=get_decimal("asd_val"), 
                # Import metadata
                is_active=True,
                import_date=import_date,
                import_batch_id=import_batch_id,
            )

            buf.append(feature)

            if len(buf) >= chunk_size:
                ExtraFeature.objects.bulk_create(buf, ignore_conflicts=True)
                results["imported"] += len(buf)
                logger.info(
                    "Loaded %s extra feature records...",
                    results["imported"]
                )
                buf.clear()

        if buf:
            ExtraFeature.objects.bulk_create(buf, ignore_conflicts=True)
            results["imported"] += len(buf)

    logger.info("Completed: Loaded %s extra feature records from %s", results["imported"], filepath)
    logger.info(
        "Invalid account numbers: %s, Skipped: %s",
        results["invalid"],
        results["skipped"],
    )
    return results


def link_orphaned_records(chunk_size: int = 5000) -> dict:
    """
    Link orphaned BuildingDetail and ExtraFeature records to their PropertyRecord.

    This handles cases where features were imported before the property was created,
    or where the property link failed during initial import.

    Returns:
        Dictionary with counts of linked records and validation stats
    """
    from .models import BuildingDetail, ExtraFeature, PropertyRecord

    results = {
        "buildings_linked": 0,
        "features_linked": 0,
        "buildings_invalid": 0,
        "features_invalid": 0,
    }

    logger.info("Linking orphaned building details...")

    # Find buildings without property links
    orphaned_buildings = BuildingDetail.objects.filter(property__isnull=True)
    total_orphaned = orphaned_buildings.count()
    logger.info("Found %s orphaned building records", total_orphaned)

    batch = []
    with transaction.atomic():
        for building in orphaned_buildings.iterator(chunk_size=chunk_size):
            if building.account_number:
                # Try to find matching property
                prop = PropertyRecord.objects.filter(
                    account_number=building.account_number
                ).first()
                if prop:
                    building.property = prop
                    batch.append(building)

                    if len(batch) >= chunk_size:
                        BuildingDetail.objects.bulk_update(batch, ["property"])
                        results["buildings_linked"] += len(batch)
                        logger.info(
                            "Linked %s building records...",
                            results["buildings_linked"],
                        )
                        batch.clear()
                else:
                    results["buildings_invalid"] += 1

        # Update remaining batch
        if batch:
            BuildingDetail.objects.bulk_update(batch, ["property"])
            results["buildings_linked"] += len(batch)

    logger.info(
        "Completed building linking: %s linked, %s invalid",
        results["buildings_linked"],
        results["buildings_invalid"],
    )

    # Now link orphaned features
    logger.info("Linking orphaned extra features...")

    orphaned_features = ExtraFeature.objects.filter(property__isnull=True)
    total_orphaned = orphaned_features.count()
    logger.info("Found %s orphaned feature records", total_orphaned)

    batch = []
    with transaction.atomic():
        for feature in orphaned_features.iterator(chunk_size=chunk_size):
            if feature.account_number:
                # Try to find matching property
                prop = PropertyRecord.objects.filter(
                    account_number=feature.account_number
                ).first()
                if prop:
                    feature.property = prop
                    batch.append(feature)

                    if len(batch) >= chunk_size:
                        ExtraFeature.objects.bulk_update(batch, ["property"])
                        results["features_linked"] += len(batch)
                        logger.info("Linked %s feature records...", results["features_linked"])
                        batch.clear()
                else:
                    results["features_invalid"] += 1

        # Update remaining batch
        if batch:
            ExtraFeature.objects.bulk_update(batch, ["property"])
            results["features_linked"] += len(batch)

    logger.info(
        "Completed feature linking: %s linked, %s invalid",
        results["features_linked"],
        results["features_invalid"],
    )
    logger.info("Total results: %s", results)

    return results


def mark_old_records_inactive(exclude_batch_id: Optional[str] = None) -> dict:
    """
    Mark old BuildingDetail and ExtraFeature records as inactive (soft delete).

    This is used during imports to deactivate old data before importing new data,
    allowing for historical tracking and rollback capability.

    Args:
        exclude_batch_id: Optional batch ID to exclude from deactivation.
                         Records with this batch_id will remain active.

    Returns:
        Dictionary with counts of deactivated records
    """
    from .models import BuildingDetail, ExtraFeature
    from django.utils import timezone

    results = {
        "buildings_deactivated": 0,
        "features_deactivated": 0,
    }

    # Mark old buildings as inactive
    query = BuildingDetail.objects.filter(is_active=True)
    if exclude_batch_id:
        query = query.exclude(import_batch_id=exclude_batch_id)

    results["buildings_deactivated"] = query.update(is_active=False)
    logger.info(
        "Marked %s building records as inactive", results["buildings_deactivated"]
    )

    # Mark old features as inactive
    query = ExtraFeature.objects.filter(is_active=True)
    if exclude_batch_id:
        query = query.exclude(import_batch_id=exclude_batch_id)

    results["features_deactivated"] = query.update(is_active=False)
    logger.info(
        "Marked %s feature records as inactive", results["features_deactivated"]
    )

    return results


def load_fixtures_room_counts(filepath: str, chunk_size: int = 5000) -> dict:
    """
    Load bedroom and bathroom counts from fixtures.txt and update BuildingDetail records.

    The fixtures.txt file contains room counts with these type codes:
    - RMB: Bedrooms
    - RMF: Full bathrooms
    - RMH: Half bathrooms

    Args:
        filepath: Path to the fixtures.txt file
        chunk_size: Number of records to process in each batch

    Returns:
        Dictionary with update statistics
    """
    from .models import BuildingDetail
    from decimal import Decimal

    reader = open_reader(filepath)

    # Dictionary to accumulate room counts by (account_number, building_number)
    # Key: (acct, bld_num), Value: {'bedrooms': X, 'bathrooms': Y, 'half_baths': Z}
    room_data = {}

    results = {
        "total_fixture_records": 0,
        "room_records_found": 0,
        "buildings_updated": 0,
        "buildings_not_found": 0,
    }

    logger.info("Loading room counts from %s", filepath)

    # First pass: collect all room counts from fixtures
    for idx, row in enumerate(reader, start=1):
        results["total_fixture_records"] += 1

        acct = (row.get("acct") or "").strip()
        bld_num_str = (row.get("bld_num") or "").strip()
        fixture_type = (row.get("type") or "").strip()
        units_str = (row.get("units") or "").strip()

        if not acct or not bld_num_str:
            continue

        # Only process room-related fixtures
        if fixture_type not in ("RMB", "RMF", "RMH"):
            continue

        results["room_records_found"] += 1

        try:
            bld_num = int(bld_num_str)
            units = Decimal(units_str) if units_str else Decimal("0")
        except (ValueError, TypeError):
            continue

        # Create key for this building
        key = (acct, bld_num)

        # Initialize if not exists
        if key not in room_data:
            room_data[key] = {"bedrooms": None, "bathrooms": None, "half_baths": None}

        # Store the appropriate room count
        if fixture_type == "RMB":  # Bedrooms
            room_data[key]["bedrooms"] = int(units)
        elif fixture_type == "RMF":  # Full bathrooms
            room_data[key]["bathrooms"] = units
        elif fixture_type == "RMH":  # Half bathrooms
            room_data[key]["half_baths"] = int(units)

        if idx % 10000 == 0:
            logger.info(
                "Processed %s fixture records, found %s buildings with room data...",
                f"{idx:,}",
                f"{len(room_data):,}",
            )

    logger.info("Found room data for %s buildings", f"{len(room_data):,}")
    logger.info("Updating BuildingDetail records...")

    # Second pass: update BuildingDetail records in batches
    keys_list = list(room_data.keys())

    for i in range(0, len(keys_list), chunk_size):
        batch_keys = keys_list[i : i + chunk_size]

        with transaction.atomic():
            for acct, bld_num in batch_keys:
                data = room_data[(acct, bld_num)]

                # Find the building record(s)
                buildings = BuildingDetail.objects.filter(
                    account_number=acct, building_number=bld_num, is_active=True
                )

                if not buildings.exists():
                    results["buildings_not_found"] += 1
                    continue

                # Update the building(s) with room counts
                update_fields = {}
                if data["bedrooms"] is not None:
                    update_fields["bedrooms"] = data["bedrooms"]

                # Calculate total bathrooms: full baths + (0.5 * half baths)
                # Store the total in bathrooms field with one decimal place
                full_baths = (
                    data["bathrooms"] if data["bathrooms"] is not None else Decimal("0")
                )
                half_baths_count = (
                    data["half_baths"] if data["half_baths"] is not None else 0
                )

                if data["bathrooms"] is not None or data["half_baths"] is not None:
                    total_bathrooms = full_baths + (
                        Decimal("0.5") * Decimal(half_baths_count)
                    )
                    update_fields["bathrooms"] = total_bathrooms

                if data["half_baths"] is not None:
                    update_fields["half_baths"] = data["half_baths"]

                if update_fields:
                    count = buildings.update(**update_fields)
                    results["buildings_updated"] += count

        if (i + chunk_size) % 50000 == 0:
            logger.info("Updated %s buildings...", f"{results['buildings_updated']:,}")

    logger.info("Fixture import complete!")
    logger.info(
        "Total fixture records processed: %s",
        f"{results['total_fixture_records']:,}",
    )
    logger.info(
        "Room records found (RMB/RMF/RMH): %s",
        f"{results['room_records_found']:,}",
    )
    logger.info("Buildings with room data: %s", f"{len(room_data):,}")
    logger.info(
        "BuildingDetail records updated: %s",
        f"{results['buildings_updated']:,}",
    )
    logger.info(
        "Buildings not found in DB: %s", f"{results['buildings_not_found']:,}"
    )

    return results
