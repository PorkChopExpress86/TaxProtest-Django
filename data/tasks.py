import os
import zipfile
import shutil
import requests
import logging
from celery import shared_task
from django.conf import settings
from .models import DownloadRecord
logger = logging.getLogger(__name__)


HCAD_URLS = [
    'https://download.hcad.org/data/CAMA/2025/Real_acct_owner.zip',
    'https://download.hcad.org/data/CAMA/2025/Real_acct_ownership_history.zip',
    'https://download.hcad.org/data/CAMA/2025/Real_building_land.zip',
    'https://download.hcad.org/data/CAMA/2025/Code_description_real.zip',
    'https://download.hcad.org/data/CAMA/2025/PP_files.zip',
    'https://download.hcad.org/data/CAMA/2025/Code_description_pp.zip',
    'https://download.hcad.org/data/CAMA/2025/Hearing_files.zip',
    'https://download.hcad.org/data/GIS/Parcels.zip',
]


def ensure_download_dir():
    download_dir = os.path.join(settings.BASE_DIR, 'downloads')
    os.makedirs(download_dir, exist_ok=True)
    return download_dir


@shared_task(bind=True)
def download_and_extract_hcad(self):
    """Download a set of HCAD ZIP files, save them to downloads/, and extract them.

    Records a DownloadRecord per file.
    """
    download_dir = ensure_download_dir()
    results = []
    for url in HCAD_URLS:
        local_name = url.split('/')[-1]
        local_path = os.path.join(download_dir, local_name)

        # stream download
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)

        # create DB record
        rec = DownloadRecord.objects.create(url=url, filename=local_name)

        # try to extract if zip
        try:
            if zipfile.is_zipfile(local_path):
                with zipfile.ZipFile(local_path, 'r') as z:
                    extract_to = os.path.join(download_dir, local_name.replace('.zip', ''))
                    os.makedirs(extract_to, exist_ok=True)
                    z.extractall(extract_to)
                rec.extracted = True
                rec.save()
        except Exception as ex:
            # Do not fail the whole task on one file; log and continue
            self.retry(exc=ex, countdown=30, max_retries=2)

        results.append({'url': url, 'local': local_path, 'extracted': rec.extracted})

    return results
import csv
import io
import requests
from celery import shared_task
from datetime import datetime

from django.conf import settings

from .models import PropertyRecord, BuildingDetail, ExtraFeature
from .etl import load_building_details, load_extra_features, load_gis_parcels, link_orphaned_records, mark_old_records_inactive


@shared_task(bind=True)
def download_extract_reload(self, source_url):
    """
    Downloads a CSV from `source_url`, parses rows, and reloads into the database.

    Expected CSV columns: address, city, zipcode, value
    """
    resp = requests.get(source_url, timeout=30)
    resp.raise_for_status()

    text = resp.content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))

    # For simplicity: remove previous records that match this source_url
    PropertyRecord.objects.filter(source_url=source_url).delete()

    objs = []
    for row in reader:
        try:
            value = row.get("value") or row.get("assessed_value") or None
            if value:
                value = float(value.replace(',', ''))
        except Exception:
            value = None

        objs.append(
            PropertyRecord(
                address=(row.get("address") or "").strip(),
                city=(row.get("city") or "").strip(),
                zipcode=(row.get("zipcode") or "").strip(),
                value=value,
                source_url=source_url,
            )
        )

    PropertyRecord.objects.bulk_create(objs)
    return {"loaded": len(objs)}


@shared_task(bind=True)
def download_and_import_building_data(self):
    """
    Monthly scheduled task to download and import building details and extra features.
    
    This task:
    1. Downloads Real_building_land.zip from HCAD
    2. Extracts the ZIP file
    3. Imports building_res.txt (building details)
    4. Imports extra_features.txt (pools, garages, etc.)
    5. Cleans up old records before importing new data
    
    Scheduled to run on the 2nd Tuesday of each month.
    """
    download_dir = ensure_download_dir()
    
    # Use current year for the URL
    current_year = datetime.now().year
    url = f'https://download.hcad.org/data/CAMA/{current_year}/Real_building_land.zip'
    
    self.update_state(state='DOWNLOADING', meta={'step': 'Downloading ZIP file'})
    
    # Download the ZIP file
    local_name = 'Real_building_land.zip'
    local_path = os.path.join(download_dir, local_name)
    
    logger.info("Downloading %s...", url)
    try:
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        logger.info("Downloaded to %s", local_path)
    except Exception as e:
        logger.error("Error downloading: %s", e)
        raise
    
    # Create download record
    rec = DownloadRecord.objects.create(url=url, filename=local_name)
    
    self.update_state(state='EXTRACTING', meta={'step': 'Extracting ZIP file'})
    
    # Extract the ZIP
    extract_dir = os.path.join(download_dir, 'Real_building_land')
    os.makedirs(extract_dir, exist_ok=True)
    
    try:
        with zipfile.ZipFile(local_path, 'r') as z:
            z.extractall(extract_dir)
        rec.extracted = True
        rec.save()
        logger.info("Extracted to %s", extract_dir)
    except Exception as e:
        logger.error("Error extracting: %s", e)
        raise
    
    # Find the building and features files
    building_file = os.path.join(extract_dir, 'building_res.txt')
    features_file = os.path.join(extract_dir, 'extra_features.txt')
    
    results = {
        'download_url': url,
        'extracted_to': extract_dir,
        'buildings_imported': 0,
        'buildings_invalid': 0,
        'features_imported': 0,
        'features_invalid': 0,
        'buildings_deactivated': 0,
        'features_deactivated': 0,
        'buildings_linked': 0,
        'features_linked': 0,
    }
    
    # Generate batch ID for this import
    batch_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # Mark old building details and extra features as inactive (soft delete)
    self.update_state(state='DEACTIVATING', meta={'step': 'Deactivating old building data'})
    logger.info("Marking old records as inactive (batch_id: %s)...", batch_id)
    deactivate_results = mark_old_records_inactive()
    results['buildings_deactivated'] = deactivate_results['buildings_deactivated']
    results['features_deactivated'] = deactivate_results['features_deactivated']
    
    # Import building details
    if os.path.exists(building_file):
        self.update_state(state='IMPORTING', meta={'step': 'Importing building details'})
        logger.info("Importing building details from %s", building_file)
        try:
            building_results = load_building_details(building_file, chunk_size=5000, import_batch_id=batch_id)
            results['buildings_imported'] = building_results['imported']
            results['buildings_invalid'] = building_results['invalid']
            logger.info("Successfully imported %s building records", building_results['imported'])
            logger.info("Invalid: %s, Skipped: %s", building_results['invalid'], building_results['skipped'])
        except Exception as e:
            logger.error("Error importing building details: %s", e)
            results['building_error'] = str(e)
    else:
        logger.warning("Building file not found at %s", building_file)
        results['building_error'] = 'File not found'
    
    # Import extra features
    if os.path.exists(features_file):
        self.update_state(state='IMPORTING', meta={'step': 'Importing extra features'})
        logger.info("Importing extra features from %s", features_file)
        try:
            feature_results = load_extra_features(features_file, chunk_size=5000, import_batch_id=batch_id)
            results['features_imported'] = feature_results['imported']
            results['features_invalid'] = feature_results['invalid']
            logger.info("Successfully imported %s feature records", feature_results['imported'])
            logger.info("Invalid: %s, Skipped: %s", feature_results['invalid'], feature_results['skipped'])
        except Exception as e:
            logger.error("Error importing extra features: %s", e)
            results['features_error'] = str(e)
    else:
        logger.warning("Features file not found at %s", features_file)
        results['features_error'] = 'File not found'
    
    # Link orphaned records (where property=None but account_number exists)
    self.update_state(state='LINKING', meta={'step': 'Linking orphaned records to properties'})
    logger.info("Linking orphaned records to their properties...")
    try:
        link_results = link_orphaned_records(chunk_size=5000)
        results['buildings_linked'] = link_results['buildings_linked']
        results['features_linked'] = link_results['features_linked']
        logger.info("Linked %s buildings and %s features", link_results['buildings_linked'], link_results['features_linked'])
    except Exception as e:
        logger.error("Error linking orphaned records: %s", e)
        results['linking_error'] = str(e)
    
    self.update_state(state='SUCCESS', meta={'step': 'Import completed'})
    
    return results


@shared_task(bind=True)
def download_and_import_gis_data(self):
    """
    Annual scheduled task to download and import GIS parcel location data.
    
    This task:
    1. Downloads Parcels.zip from HCAD (GIS data)
    2. Extracts the ZIP file
    3. Imports shapefile data to update latitude/longitude for all properties
    
    This runs once per year since property locations rarely change.
    Can also be triggered manually via Django admin or management command.
    
    Scheduled to run annually on January 15th at 3 AM.
    """
    download_dir = ensure_download_dir()
    
    # Use current year for the URL
    current_year = datetime.now().year
    url = f'https://download.hcad.org/data/GIS/Parcels.zip'
    
    self.update_state(state='DOWNLOADING', meta={'step': 'Downloading GIS Parcels ZIP'})
    
    # Download the ZIP file
    local_name = 'Parcels.zip'
    local_path = os.path.join(download_dir, local_name)
    
    logger.info("Downloading %s...", url)
    try:
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        logger.info("Downloaded to %s", local_path)
    except Exception as e:
        logger.error("Error downloading: %s", e)
        raise
    
    # Create download record
    rec = DownloadRecord.objects.create(url=url, filename=local_name)
    
    self.update_state(state='EXTRACTING', meta={'step': 'Extracting GIS ZIP file'})
    
    # Extract the ZIP
    extract_dir = os.path.join(download_dir, 'Parcels')
    os.makedirs(extract_dir, exist_ok=True)
    
    try:
        with zipfile.ZipFile(local_path, 'r') as z:
            z.extractall(extract_dir)
        rec.extracted = True
        rec.save()
        logger.info("Extracted to %s", extract_dir)
    except Exception as e:
        logger.error("Error extracting: %s", e)
        raise
    
    # Find the shapefile
    shapefile_path = os.path.join(extract_dir, 'Gis', 'pdata', 'ParcelsCity', 'ParcelsCity.shp')
    
    results = {
        'download_url': url,
        'extracted_to': extract_dir,
        'properties_updated': 0,
    }
    
    # Import GIS data
    if os.path.exists(shapefile_path):
        self.update_state(state='IMPORTING', meta={'step': 'Importing GIS location data'})
        logger.info("Importing GIS data from %s", shapefile_path)
        try:
            updated_count = load_gis_parcels(shapefile_path, chunk_size=5000)
            results['properties_updated'] = updated_count
            logger.info("Successfully updated %s properties with location data", updated_count)
        except Exception as e:
            logger.error("Error importing GIS data: %s", e)
            results['gis_error'] = str(e)
            raise
    else:
        error_msg = f'Shapefile not found at {shapefile_path}'
        logger.error("%s", error_msg)
        results['gis_error'] = error_msg
        raise FileNotFoundError(error_msg)
    
    self.update_state(state='SUCCESS', meta={'step': 'GIS import completed'})
    
    return results
