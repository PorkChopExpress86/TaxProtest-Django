"""
Management command to run complete data import: properties, buildings, and GIS data.

Usage:
    python manage.py import_all_data [--skip-property] [--skip-building] [--skip-gis]
"""

from django.core.management.base import BaseCommand
from django.core.management import call_command
import sys


class Command(BaseCommand):
    help = 'Run complete data import: property records, building details, and GIS coordinates'

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-property',
            action='store_true',
            help='Skip property records import',
        )
        parser.add_argument(
            '--skip-building',
            action='store_true',
            help='Skip building data import',
        )
        parser.add_argument(
            '--skip-gis',
            action='store_true',
            help='Skip GIS data import',
        )
        parser.add_argument(
            '--skip-download',
            action='store_true',
            help='Skip downloading files (use existing)',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('='*70))
        self.stdout.write(self.style.SUCCESS('COMPLETE DATA IMPORT'))
        self.stdout.write(self.style.SUCCESS('='*70))
        
        # Step 1: Import property records
        if not options['skip_property']:
            self.stdout.write(self.style.SUCCESS('\n[1/3] Importing property records...'))
            self.stdout.write('-' * 70)
            try:
                call_command('load_hcad_real_acct')
                self.stdout.write(self.style.SUCCESS('✓ Property records imported successfully\n'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'✗ Property import failed: {e}\n'))
                return
        else:
            self.stdout.write(self.style.WARNING('\n[1/3] Skipping property records import\n'))
        
        # Step 2: Import building details and features
        if not options['skip_building']:
            self.stdout.write(self.style.SUCCESS('[2/3] Importing building data...'))
            self.stdout.write('-' * 70)
            try:
                if options['skip_download']:
                    call_command('import_building_data', '--skip-download')
                else:
                    call_command('import_building_data')
                self.stdout.write(self.style.SUCCESS('✓ Building data imported successfully\n'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'✗ Building import failed: {e}\n'))
                # Continue anyway - GIS can still be imported
        else:
            self.stdout.write(self.style.WARNING('[2/3] Skipping building data import\n'))
        
        # Step 3: Import GIS coordinates
        if not options['skip_gis']:
            self.stdout.write(self.style.SUCCESS('[3/3] Importing GIS coordinates...'))
            self.stdout.write('-' * 70)
            try:
                if options['skip_download']:
                    call_command('load_gis_data', '--skip-download')
                else:
                    call_command('load_gis_data')
                self.stdout.write(self.style.SUCCESS('✓ GIS data imported successfully\n'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'✗ GIS import failed: {e}\n'))
        else:
            self.stdout.write(self.style.WARNING('[3/3] Skipping GIS data import\n'))
        
        # Summary
        self.stdout.write(self.style.SUCCESS('='*70))
        self.stdout.write(self.style.SUCCESS('IMPORT COMPLETE'))
        self.stdout.write(self.style.SUCCESS('='*70))
        
        # Show statistics
        from data.models import PropertyRecord, BuildingDetail, ExtraFeature
        
        total_props = PropertyRecord.objects.count()
        total_buildings = BuildingDetail.objects.filter(is_active=True).count()
        total_features = ExtraFeature.objects.filter(is_active=True).count()
        props_with_coords = PropertyRecord.objects.filter(latitude__isnull=False).count()
        
        self.stdout.write(f'\nDatabase Statistics:')
        self.stdout.write(f'  Properties:        {total_props:>10,}')
        self.stdout.write(f'  Buildings:         {total_buildings:>10,}')
        self.stdout.write(f'  Features:          {total_features:>10,}')
        self.stdout.write(f'  With coordinates:  {props_with_coords:>10,} ({props_with_coords/total_props*100:.1f}%)')
        self.stdout.write('\n' + '='*70 + '\n')
        # Explicit final marker for external monitors and then exit cleanly
        self.stdout.write(self.style.SUCCESS('IMPORT COMPLETE - EXITING'))
        # Ensure process exits with a success status so orchestrators see completion
        sys.exit(0)
