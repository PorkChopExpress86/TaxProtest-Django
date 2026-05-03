"""
Management command to download and load GIS parcel data from HCAD.
"""
import os
import zipfile
from pathlib import Path

import requests
from django.core.management.base import BaseCommand
from django.conf import settings
from data.etl import load_gis_parcels


def find_preferred_shapefile(extract_dir: str) -> str | None:
    """Return the best shapefile candidate from an extracted HCAD GIS archive.

    Recent HCAD parcel archives may contain both a top-level `Parcels.shp` and a
    nested `ParcelsCity.shp`. The nested `ParcelsCity.shp` is the primary parcel
    layer with usable parcel identifiers, so prefer it when present.
    """
    shapefiles: list[str] = []

    for root, dirs, files in os.walk(extract_dir):
        for file in files:
            if file.endswith('.shp'):
                shapefiles.append(os.path.join(root, file))

    if not shapefiles:
        return None

    def priority(path: str) -> tuple[int, int, int]:
        normalized = path.replace('\\', '/').lower()
        name = os.path.basename(normalized)
        return (
            2 if name == 'parcelscity.shp' else 1 if 'parcelscity' in name else 0,
            1 if '/gis/pdata/' in normalized else 0,
            len(normalized),
        )

    return max(shapefiles, key=priority)


class Command(BaseCommand):
    help = 'Download and load GIS parcel data from HCAD'

    def add_arguments(self, parser):
        parser.add_argument(
            '--url',
            type=str,
            default='https://download.hcad.org/data/GIS/Parcels.zip',
            help='URL to download GIS Parcels.zip from'
        )
        parser.add_argument(
            '--skip-download',
            action='store_true',
            help='Skip download and use existing extracted files'
        )
        parser.add_argument(
            '--no-refresh-readiness',
            action='store_true',
            help='Skip readiness recomputation after GIS import',
        )

    def handle(self, *args, **options):
        url = options['url']
        skip_download = options['skip_download']
        
        # Setup download directory
        download_dir = Path(settings.HCAD_DOWNLOAD_DIR)
        extract_root = Path(settings.HCAD_EXTRACT_DIR)
        download_dir.mkdir(parents=True, exist_ok=True)
        extract_root.mkdir(parents=True, exist_ok=True)
        
        zip_filename = 'Parcels.zip'
        zip_path = download_dir / zip_filename
        extract_dir = extract_root / 'Parcels'
        
        if not skip_download:
            self.stdout.write(self.style.SUCCESS(f'Downloading GIS data from {url}...'))
            
            # Download the zip file with progress
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()
            total_length = int(response.headers.get('content-length') or 0)
            downloaded = 0
            
            with zip_path.open('wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_length > 0:
                        percent = int(100 * downloaded / total_length)
                        if downloaded % (5 * 1024 * 1024) < 8192:  # Every 5MB
                            self.stdout.write(f'  ... {percent}% ({downloaded//(1024*1024)} MB)', ending='\r')
                            self.stdout.flush()
            
            self.stdout.write(f'\nDownloaded {downloaded//(1024*1024)} MB to {zip_filename}')
            
            # Extract the zip file
            self.stdout.write(self.style.SUCCESS(f'Extracting {zip_filename}...'))
            extract_dir.mkdir(parents=True, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            
            self.stdout.write(self.style.SUCCESS(f'Extracted to {extract_dir}'))
        
        # Find shapefile in extracted directory
        shapefile_path = find_preferred_shapefile(str(extract_dir))
        
        if not shapefile_path:
            self.stdout.write(self.style.ERROR(f'No shapefile (.shp) found in {extract_dir}'))
            return
        
        self.stdout.write(self.style.SUCCESS(f'Found shapefile: {shapefile_path}'))
        self.stdout.write(self.style.SUCCESS('Loading GIS data into database...'))
        
        # Load the GIS data
        try:
            count = load_gis_parcels(
                shapefile_path,
                refresh_readiness=not options.get('no_refresh_readiness', False),
            )
            self.stdout.write(self.style.SUCCESS(f'Successfully updated {count} property records with GIS coordinates'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error loading GIS data: {str(e)}'))
            raise
