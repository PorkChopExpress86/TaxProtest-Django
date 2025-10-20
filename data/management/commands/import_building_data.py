"""
Management command to manually trigger the building data import task.

Usage:
    python manage.py import_building_data [--async]
"""

from django.core.management.base import BaseCommand
from data.tasks import download_and_import_building_data


class Command(BaseCommand):
    help = 'Manually trigger the building data import task'

    def add_arguments(self, parser):
        parser.add_argument(
            '--async',
            action='store_true',
            help='Run the task asynchronously via Celery (default is synchronous)',
        )
        parser.add_argument(
            '--skip-download',
            action='store_true',
            help='Skip downloading and extracting; use existing files',
        )
        parser.add_argument(
            '--with-gis',
            action='store_true',
            help='Also import GIS coordinate data after building import',
        )

    def handle(self, *args, **options):
        if options['async']:
            # Run via Celery (asynchronous)
            self.stdout.write(self.style.SUCCESS('Sending task to Celery worker...'))
            task = download_and_import_building_data.delay()
            self.stdout.write(self.style.SUCCESS(f'Task queued successfully!'))
            self.stdout.write(self.style.SUCCESS(f'Task ID: {task.id}'))
            self.stdout.write(self.style.SUCCESS(f'Monitor with: docker compose logs -f worker'))
        else:
            # Run synchronously (direct call, no Celery)
            self.stdout.write(self.style.SUCCESS('Starting building data import (synchronous)...'))
            
            # Import the actual function logic without the decorator
            import os
            import zipfile
            import shutil
            import requests
            from datetime import datetime
            from django.conf import settings
            from data.models import DownloadRecord, BuildingDetail, ExtraFeature
            from data.etl import load_building_details, load_extra_features, mark_old_records_inactive, link_orphaned_records, load_fixtures_room_counts
            
            download_dir = os.path.join(settings.BASE_DIR, 'downloads')
            os.makedirs(download_dir, exist_ok=True)
            
            current_year = datetime.now().year
            url = f'https://download.hcad.org/data/CAMA/{current_year}/Real_building_land.zip'
            local_name = 'Real_building_land.zip'
            local_path = os.path.join(download_dir, local_name)
            extract_dir = os.path.join(download_dir, 'Real_building_land')
            
            if options.get('skip_download'):
                self.stdout.write('Skipping download, using existing files...')
                if not os.path.exists(extract_dir):
                    raise Exception(f'Extract directory not found: {extract_dir}. Remove --skip-download to download.')
            else:
                self.stdout.write(f'Downloading {url}...')
                with requests.get(url, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with open(local_path, 'wb') as f:
                        shutil.copyfileobj(r.raw, f)
                self.stdout.write(self.style.SUCCESS(f'Downloaded to {local_path}'))
                
                rec = DownloadRecord.objects.create(url=url, filename=local_name)
                
                os.makedirs(extract_dir, exist_ok=True)
                
                self.stdout.write('Extracting ZIP file...')
                with zipfile.ZipFile(local_path, 'r') as z:
                    z.extractall(extract_dir)
                rec.extracted = True
                rec.save()
                self.stdout.write(self.style.SUCCESS(f'Extracted to {extract_dir}'))
            
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
                'rooms_updated': 0,
            }
            
            # Generate batch ID for this import
            batch_id = datetime.now().strftime('%Y%m%d_%H%M%S')
            
            # Mark old data as inactive (soft delete)
            self.stdout.write('Marking old building data as inactive...')
            deactivate_results = mark_old_records_inactive()
            results['buildings_deactivated'] = deactivate_results['buildings_deactivated']
            results['features_deactivated'] = deactivate_results['features_deactivated']
            self.stdout.write(f'Marked {results["buildings_deactivated"]} buildings and {results["features_deactivated"]} features as inactive')
            
            # Import building details
            if os.path.exists(building_file):
                self.stdout.write(f'Importing building details from {building_file}...')
                try:
                    building_results = load_building_details(building_file, chunk_size=5000, import_batch_id=batch_id)
                    results['buildings_imported'] = building_results['imported']
                    results['buildings_invalid'] = building_results['invalid']
                    self.stdout.write(self.style.SUCCESS(f'Imported {building_results["imported"]} building records'))
                    self.stdout.write(f'Invalid: {building_results["invalid"]}, Skipped: {building_results["skipped"]}')
                except Exception as e:
                    results['building_error'] = str(e)
                    self.stdout.write(self.style.ERROR(f'Error importing buildings: {e}'))
            else:
                results['building_error'] = 'File not found'
                self.stdout.write(self.style.WARNING(f'Building file not found at {building_file}'))
            
            # Import extra features
            if os.path.exists(features_file):
                self.stdout.write(f'Importing extra features from {features_file}...')
                try:
                    feature_results = load_extra_features(features_file, chunk_size=5000, import_batch_id=batch_id)
                    results['features_imported'] = feature_results['imported']
                    results['features_invalid'] = feature_results['invalid']
                    self.stdout.write(self.style.SUCCESS(f'Imported {feature_results["imported"]} feature records'))
                    self.stdout.write(f'Invalid: {feature_results["invalid"]}, Skipped: {feature_results["skipped"]}')
                except Exception as e:
                    results['features_error'] = str(e)
                    self.stdout.write(self.style.ERROR(f'Error importing features: {e}'))
            else:
                results['features_error'] = 'File not found'
                self.stdout.write(self.style.WARNING(f'Features file not found at {features_file}'))
            
            # Link orphaned records
            self.stdout.write('Linking orphaned records to properties...')
            try:
                link_results = link_orphaned_records(chunk_size=5000)
                results['buildings_linked'] = link_results['buildings_linked']
                results['features_linked'] = link_results['features_linked']
                self.stdout.write(self.style.SUCCESS(f'Linked {link_results["buildings_linked"]} buildings and {link_results["features_linked"]} features'))
            except Exception as e:
                results['linking_error'] = str(e)
                self.stdout.write(self.style.ERROR(f'Error linking orphaned records: {e}'))
            
            # Load room counts from fixtures
            if os.path.exists(fixtures_file):
                self.stdout.write(f'Loading bedroom/bathroom counts from {fixtures_file}...')
                try:
                    fixtures_results = load_fixtures_room_counts(fixtures_file, chunk_size=5000)
                    results['rooms_updated'] = fixtures_results['buildings_updated']
                    self.stdout.write(self.style.SUCCESS(f'Updated {fixtures_results["buildings_updated"]} buildings with room counts'))
                except Exception as e:
                    results['fixtures_error'] = str(e)
                    self.stdout.write(self.style.ERROR(f'Error loading room counts: {e}'))
            else:
                self.stdout.write(self.style.WARNING(f'Fixtures file not found at {fixtures_file}'))
            
            self.stdout.write(self.style.SUCCESS('\n' + '='*70))
            self.stdout.write(self.style.SUCCESS('Building data import completed!'))
            self.stdout.write(self.style.SUCCESS(f'Batch ID: {batch_id}'))
            self.stdout.write(self.style.SUCCESS(f'Buildings deactivated: {results["buildings_deactivated"]}'))
            self.stdout.write(self.style.SUCCESS(f'Features deactivated: {results["features_deactivated"]}'))
            self.stdout.write(self.style.SUCCESS(f'Buildings imported: {results["buildings_imported"]}'))
            self.stdout.write(self.style.SUCCESS(f'Features imported: {results["features_imported"]}'))
            self.stdout.write(self.style.SUCCESS(f'Buildings linked: {results["buildings_linked"]}'))
            self.stdout.write(self.style.SUCCESS(f'Features linked: {results["features_linked"]}'))
            self.stdout.write(self.style.SUCCESS(f'Rooms updated: {results.get("rooms_updated", 0)}'))
            self.stdout.write(self.style.SUCCESS('='*70))
            
            # Optional: Also import GIS data
            if options.get('with_gis'):
                self.stdout.write(self.style.SUCCESS('\n' + '='*70))
                self.stdout.write(self.style.SUCCESS('Importing GIS coordinate data...'))
                self.stdout.write(self.style.SUCCESS('='*70))
                from django.core.management import call_command
                try:
                    if options.get('skip_download'):
                        call_command('load_gis_data', '--skip-download')
                    else:
                        call_command('load_gis_data')
                    self.stdout.write(self.style.SUCCESS('✓ GIS data import completed'))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'✗ GIS import failed: {e}'))
