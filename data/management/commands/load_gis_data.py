"""
Management command to download and load GIS parcel data from HCAD.
"""
import os
import zipfile
import requests
from django.core.management.base import BaseCommand
from django.conf import settings
from data.etl import load_gis_parcels


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

    def handle(self, *args, **options):
        url = options['url']
        skip_download = options['skip_download']
        
        # Setup download directory
        download_dir = os.path.join(settings.BASE_DIR, 'downloads')
        os.makedirs(download_dir, exist_ok=True)
        
        zip_filename = 'Parcels.zip'
        zip_path = os.path.join(download_dir, zip_filename)
        extract_dir = os.path.join(download_dir, 'Parcels')
        
        if not skip_download:
            self.stdout.write(self.style.SUCCESS(f'Downloading GIS data from {url}...'))
            
            # Download the zip file
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()
            
            with open(zip_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            self.stdout.write(self.style.SUCCESS(f'Downloaded {zip_filename}'))
            
            # Extract the zip file
            self.stdout.write(self.style.SUCCESS(f'Extracting {zip_filename}...'))
            os.makedirs(extract_dir, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            
            self.stdout.write(self.style.SUCCESS(f'Extracted to {extract_dir}'))
        
        # Find shapefile in extracted directory
        shapefile_path = None
        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                if file.endswith('.shp'):
                    shapefile_path = os.path.join(root, file)
                    break
            if shapefile_path:
                break
        
        if not shapefile_path:
            self.stdout.write(self.style.ERROR(f'No shapefile (.shp) found in {extract_dir}'))
            return
        
        self.stdout.write(self.style.SUCCESS(f'Found shapefile: {shapefile_path}'))
        self.stdout.write(self.style.SUCCESS('Loading GIS data into database...'))
        
        # Load the GIS data
        try:
            count = load_gis_parcels(shapefile_path)
            self.stdout.write(self.style.SUCCESS(f'Successfully updated {count} property records with GIS coordinates'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error loading GIS data: {str(e)}'))
            raise
