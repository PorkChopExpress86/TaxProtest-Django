"""
Management command to load bedroom and bathroom counts from fixtures.txt.
"""
import os
from django.core.management.base import BaseCommand
from django.conf import settings
from data.etl import load_fixtures_room_counts


class Command(BaseCommand):
    help = 'Load bedroom and bathroom counts from fixtures.txt file'

    def add_arguments(self, parser):
        parser.add_argument(
            '--fixtures-file',
            type=str,
            default='downloads/Real_building_land/fixtures.txt',
            help='Path to fixtures.txt file (relative to BASE_DIR)'
        )
        parser.add_argument(
            '--chunk-size',
            type=int,
            default=5000,
            help='Number of records to process in each batch (default: 5000)'
        )

    def handle(self, *args, **options):
        fixtures_file = options['fixtures_file']
        chunk_size = options['chunk_size']
        
        # Convert relative path to absolute
        if not os.path.isabs(fixtures_file):
            fixtures_file = os.path.join(settings.BASE_DIR, fixtures_file)
        
        if not os.path.exists(fixtures_file):
            self.stdout.write(self.style.ERROR(
                f'Fixtures file not found: {fixtures_file}\n'
                f'Make sure Real_building_land.zip has been downloaded and extracted.'
            ))
            return
        
        self.stdout.write(self.style.SUCCESS(f'Loading room counts from {fixtures_file}...'))
        
        try:
            results = load_fixtures_room_counts(fixtures_file, chunk_size=chunk_size)
            
            self.stdout.write(self.style.SUCCESS('\n' + '='*70))
            self.stdout.write(self.style.SUCCESS('Room count import completed!'))
            self.stdout.write(self.style.SUCCESS(f'Total fixture records: {results["total_fixture_records"]:,}'))
            self.stdout.write(self.style.SUCCESS(f'Room records (RMB/RMF/RMH): {results["room_records_found"]:,}'))
            self.stdout.write(self.style.SUCCESS(f'BuildingDetail records updated: {results["buildings_updated"]:,}'))
            
            if results['buildings_not_found'] > 0:
                self.stdout.write(self.style.WARNING(
                    f'Buildings not found: {results["buildings_not_found"]:,}'
                ))
            
            self.stdout.write(self.style.SUCCESS('='*70))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error loading room counts: {str(e)}'))
            raise
