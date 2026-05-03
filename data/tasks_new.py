"""
Celery tasks for HCAD data import using the new ETL pipeline.

These tasks provide async versions of the ETL pipeline operations
for use with Celery Beat scheduling and manual triggering.
"""

import os
import zipfile
import shutil
import requests
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from celery import shared_task
from django.conf import settings

from .models import DownloadRecord, PropertyRecord, BuildingDetail, ExtraFeature
from .etl import (
    load_building_details, load_extra_features, load_gis_parcels,
    link_orphaned_records, mark_old_records_inactive, load_fixtures_room_counts
)

logger = logging.getLogger(__name__)


# =============================================================================
# Legacy Tasks (kept for backward compatibility during migration)
# =============================================================================

HCAD_ARCHIVE_SOURCES = [
    {'filename': 'Real_acct_owner.zip', 'required': True, 'timeout': 300},
    {'filename': 'Real_acct_ownership_history.zip', 'required': False, 'timeout': 300},
    {'filename': 'Real_building_land.zip', 'required': True, 'timeout': 300},
    {'filename': 'Code_description_real.zip', 'required': False, 'timeout': 120},
    {'filename': 'PP_files.zip', 'required': False, 'timeout': 300},
    {'filename': 'Code_description_pp.zip', 'required': False, 'timeout': 120},
    {'filename': 'Hearing_files.zip', 'required': False, 'timeout': 300},
    {
        'filename': 'Parcels.zip',
        'required': True,
        'timeout': 600,
        'url': 'https://download.hcad.org/data/GIS/Parcels.zip',
    },
]


def candidate_cama_years(reference_year: int | None = None) -> list[int]:
    """Return candidate CAMA data years, preferring the current year then previous year."""
    year = reference_year or datetime.now().year
    years = [year]
    if year > 2000:
        years.append(year - 1)
    return years


def build_archive_candidate_urls(source: dict[str, Any], reference_year: int | None = None) -> list[str]:
    """Build candidate download URLs for an HCAD archive source."""
    explicit_url = source.get('url')
    if explicit_url:
        return [explicit_url]

    filename = source['filename']
    return [
        f'https://download.hcad.org/data/CAMA/{year}/{filename}'
        for year in candidate_cama_years(reference_year)
    ]


def download_archive_with_fallback(
    source: dict[str, Any],
    download_dir: str,
    reference_year: int | None = None,
) -> tuple[str | None, str]:
    """Download an archive, falling back across candidate URLs when needed.

    Returns the URL that succeeded (or ``None`` for skipped optional archives)
    along with the local path used for the download.
    """
    filename = source['filename']
    local_path = os.path.join(download_dir, filename)
    candidate_urls = build_archive_candidate_urls(source, reference_year)
    last_error: Exception | None = None

    for url in candidate_urls:
        try:
            with requests.get(url, stream=True, timeout=source.get('timeout', 300)) as r:
                r.raise_for_status()
                with open(local_path, 'wb') as f:
                    shutil.copyfileobj(r.raw, f)
            return url, local_path
        except requests.RequestException as exc:
            last_error = exc
            if os.path.exists(local_path):
                os.remove(local_path)

            status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status_code == 404:
                logger.warning('Archive %s not available at %s', filename, url)
            else:
                logger.warning('Failed downloading %s from %s: %s', filename, url, exc)

    if source.get('required', True):
        if last_error:
            raise last_error
        raise FileNotFoundError(f'No candidate URLs succeeded for required archive {filename}')

    logger.warning('Skipping optional archive %s after trying %s', filename, candidate_urls)
    return None, local_path


def ensure_download_dir():
    download_dir = Path(settings.HCAD_DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    return os.fspath(download_dir)


def ensure_extract_dir() -> str:
    extract_dir = Path(settings.HCAD_EXTRACT_DIR)
    extract_dir.mkdir(parents=True, exist_ok=True)
    return os.fspath(extract_dir)


@shared_task(bind=True)
def download_and_extract_hcad(self):
    """Download a set of HCAD ZIP files and extract them into the configured runtime dirs.

    Records a DownloadRecord per file.
    """
    download_dir = ensure_download_dir()
    extract_root = ensure_extract_dir()
    results = []
    reference_year = datetime.now().year

    for source in HCAD_ARCHIVE_SOURCES:
        local_name = source['filename']
        downloaded_url, local_path = download_archive_with_fallback(
            source,
            download_dir,
            reference_year=reference_year,
        )

        if downloaded_url is None:
            results.append(
                {
                    'filename': local_name,
                    'url': None,
                    'local': local_path,
                    'extracted': False,
                    'optional': True,
                    'skipped': True,
                }
            )
            continue

        # create DB record
        rec = DownloadRecord.objects.create(url=downloaded_url, filename=local_name)

        # try to extract if zip
        try:
            if zipfile.is_zipfile(local_path):
                with zipfile.ZipFile(local_path, 'r') as z:
                    extract_to = os.path.join(
                        extract_root, local_name.replace('.zip', '')
                    )
                    os.makedirs(extract_to, exist_ok=True)
                    z.extractall(extract_to)
                rec.extracted = True
                rec.save()
        except Exception as ex:
            if source.get('required', True):
                raise
            logger.warning('Skipping optional archive %s after extraction error: %s', local_name, ex)

        results.append(
            {
                'filename': local_name,
                'url': downloaded_url,
                'local': local_path,
                'extracted': rec.extracted,
                'optional': not source.get('required', True),
                'skipped': False,
            }
        )

    return results


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
    extract_root = ensure_extract_dir()
    
    self.update_state(state='DOWNLOADING', meta={'step': 'Downloading ZIP file'})
    
    # Download the ZIP file
    source = next(s for s in HCAD_ARCHIVE_SOURCES if s['filename'] == 'Real_building_land.zip')

    logger.info("Downloading %s...", source['filename'])
    try:
        url, local_path = download_archive_with_fallback(source, download_dir)
        logger.info("Downloaded %s to %s", url, local_path)
    except Exception as e:
        logger.error("Error downloading: %s", e)
        raise
    
    # Create download record
    rec = DownloadRecord.objects.create(url=url, filename=source['filename'])
    
    self.update_state(state='EXTRACTING', meta={'step': 'Extracting ZIP file'})
    
    # Extract the ZIP
    extract_dir = os.path.join(extract_root, 'Real_building_land')
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
    fixtures_file = os.path.join(extract_dir, 'fixtures.txt')
    
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
    
    # Load room counts from fixtures
    if os.path.exists(fixtures_file):
        self.update_state(state='IMPORTING', meta={'step': 'Loading room counts from fixtures'})
        logger.info("Loading room counts from %s", fixtures_file)
        try:
            room_results = load_fixtures_room_counts(fixtures_file)
            results['room_counts_updated'] = room_results.get('buildings_updated', 0)
            logger.info("Updated %s buildings with room counts", room_results.get('buildings_updated', 0))
        except Exception as e:
            logger.error("Error loading room counts: %s", e)
            results['room_counts_error'] = str(e)
    
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
    extract_root = ensure_extract_dir()
    
    url = 'https://download.hcad.org/data/GIS/Parcels.zip'
    
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
    extract_dir = os.path.join(extract_root, 'Parcels')
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


# =============================================================================
# New ETL Pipeline Tasks
# =============================================================================

@shared_task(bind=True)
def run_etl_pipeline(self, skip_download=False, skip_extract=False, skip_load=False, data_year=None):
    """
    Run the full ETL pipeline using the new modular system.
    
    Args:
        skip_download: Skip downloading files (use existing)
        skip_extract: Skip extracting archives (use existing extracted files)
        skip_load: Skip loading to database (dry run)
        data_year: Override data year (default: current year)
    
    Returns:
        Dict with pipeline execution results
    """
    from .etl_pipeline import ETLConfig, ETLOrchestrator
    
    logger.info("Starting ETL pipeline task...")
    
    self.update_state(state='INITIALIZING', meta={'step': 'Initializing pipeline'})
    
    # Configure pipeline
    config = ETLConfig.from_env()
    if data_year:
        config.data_year = data_year
    
    orchestrator = ETLOrchestrator(config)
    
    # Execute pipeline with state updates
    def update_stage_state(stage_result):
        self.update_state(
            state=stage_result.stage.value.upper(),
            meta={'step': f'{stage_result.stage.value} stage'}
        )
    
    # Register callback for state updates
    from .etl_pipeline.orchestrator import PipelineStage
    for stage in PipelineStage:
        orchestrator.register_stage_callback(stage, update_stage_state)
    
    # Run pipeline
    result = orchestrator.execute(
        skip_download=skip_download,
        skip_extract=skip_extract,
        skip_load=skip_load,
    )
    
    self.update_state(state='SUCCESS', meta={'step': 'Pipeline completed'})
    
    return result.to_dict()


@shared_task(bind=True)
def download_hcad_data(self, include_optional=False, data_year=None):
    """
    Download HCAD data files using the new ETL pipeline.
    
    Args:
        include_optional: Include optional data sources
        data_year: Override data year (default: current year)
    
    Returns:
        Dict with download results
    """
    from .etl_pipeline import ETLConfig, DownloadManager
    
    logger.info("Starting HCAD download task...")
    
    self.update_state(state='DOWNLOADING', meta={'step': 'Downloading HCAD files'})
    
    config = ETLConfig.from_env()
    if data_year:
        config.data_year = data_year
    
    manager = DownloadManager(config)
    
    if include_optional:
        sources = config.get_all_sources()
    else:
        sources = config.get_required_sources()
    
    results = manager.download_batch(sources)
    
    success_count = sum(1 for r in results if r.success)
    total_bytes = sum(r.bytes_downloaded for r in results)
    
    self.update_state(state='SUCCESS', meta={'step': 'Download completed'})
    
    return {
        'sources_total': len(sources),
        'sources_success': success_count,
        'bytes_downloaded': total_bytes,
        'results': [
            {
                'name': r.source.name,
                'success': r.success,
                'bytes': r.bytes_downloaded,
                'error': r.error,
            }
            for r in results
        ],
    }


@shared_task(bind=True)
def extract_hcad_data(self):
    """
    Extract downloaded HCAD archives using the new ETL pipeline.
    
    Returns:
        Dict with extraction results
    """
    from .etl_pipeline import ETLConfig, DownloadManager, ExtractManager
    
    logger.info("Starting HCAD extraction task...")
    
    self.update_state(state='EXTRACTING', meta={'step': 'Extracting HCAD archives'})
    
    config = ETLConfig.from_env()
    
    download_manager = DownloadManager(config)
    extract_manager = ExtractManager(config)
    
    # Only extract downloaded sources
    sources = [s for s in config.get_all_sources() if download_manager.is_downloaded(s)]
    
    results = extract_manager.extract_batch(sources)
    
    success_count = sum(1 for r in results if r.success)
    total_files = sum(len(r.files_extracted or []) for r in results)
    
    self.update_state(state='SUCCESS', meta={'step': 'Extraction completed'})
    
    return {
        'archives_total': len(sources),
        'archives_success': success_count,
        'files_extracted': total_files,
        'results': [
            {
                'name': r.source.name,
                'success': r.success,
                'files': len(r.files_extracted or []),
                'error': r.error,
            }
            for r in results
        ],
    }
