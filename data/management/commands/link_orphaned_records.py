"""
Management command to manually link orphaned building/feature records to properties.

Usage:
    python manage.py link_orphaned_records
"""

from django.core.management.base import BaseCommand
from data.etl import link_orphaned_records


class Command(BaseCommand):
    help = 'Link orphaned BuildingDetail and ExtraFeature records to their PropertyRecords'

    def add_arguments(self, parser):
        parser.add_argument(
            '--chunk-size',
            type=int,
            default=5000,
            help='Number of records to process in each batch (default: 5000)',
        )

    def handle(self, *args, **options):
        chunk_size = options['chunk_size']
        
        self.stdout.write(self.style.SUCCESS('Starting orphaned record linking...'))
        self.stdout.write(f'Chunk size: {chunk_size}')
        
        try:
            results = link_orphaned_records(chunk_size=chunk_size)
            
            self.stdout.write(self.style.SUCCESS('\n' + '='*70))
            self.stdout.write(self.style.SUCCESS('Linking completed!'))
            self.stdout.write(self.style.SUCCESS(f'Buildings linked: {results["buildings_linked"]}'))
            self.stdout.write(self.style.SUCCESS(f'Buildings invalid: {results["buildings_invalid"]}'))
            self.stdout.write(self.style.SUCCESS(f'Features linked: {results["features_linked"]}'))
            self.stdout.write(self.style.SUCCESS(f'Features invalid: {results["features_invalid"]}'))
            self.stdout.write(self.style.SUCCESS('='*70))
            
            if results['buildings_invalid'] > 0 or results['features_invalid'] > 0:
                self.stdout.write(self.style.WARNING(
                    '\nNote: Invalid records have account_numbers that do not match any PropertyRecord.'
                ))
                self.stdout.write(self.style.WARNING(
                    'These may need to be investigated or cleaned up manually.'
                ))
        
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error during linking: {e}'))
            raise
