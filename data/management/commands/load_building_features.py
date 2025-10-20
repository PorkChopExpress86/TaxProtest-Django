"""
Management command to load building details and extra features from HCAD.
"""
import os
from django.core.management.base import BaseCommand
from django.conf import settings
from data.etl import load_building_details, load_extra_features


class Command(BaseCommand):
    help = 'Load building details and extra features from HCAD Real_building_land files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--building-file',
            type=str,
            default='downloads/Real_building_land/building_res.txt',
            help='Path to building_res.txt file (relative to BASE_DIR)'
        )
        parser.add_argument(
            '--features-file',
            type=str,
            default='downloads/Real_building_land/extra_features.txt',
            help='Path to extra_features.txt file (relative to BASE_DIR)'
        )
        parser.add_argument(
            '--skip-buildings',
            action='store_true',
            help='Skip loading building details'
        )
        parser.add_argument(
            '--skip-features',
            action='store_true',
            help='Skip loading extra features'
        )

    def handle(self, *args, **options):
        building_file = options['building_file']
        features_file = options['features_file']
        skip_buildings = options['skip_buildings']
        skip_features = options['skip_features']
        
        # Convert relative paths to absolute
        if not os.path.isabs(building_file):
            building_file = os.path.join(settings.BASE_DIR, building_file)
        if not os.path.isabs(features_file):
            features_file = os.path.join(settings.BASE_DIR, features_file)
        
        # Load building details
        if not skip_buildings:
            if not os.path.exists(building_file):
                self.stdout.write(self.style.WARNING(
                    f'Building file not found: {building_file}\n'
                    f'Make sure Real_building_land.zip has been downloaded and extracted.'
                ))
            else:
                self.stdout.write(self.style.SUCCESS(f'Loading building details from {building_file}...'))
                try:
                    count = load_building_details(building_file)
                    self.stdout.write(self.style.SUCCESS(f'Successfully loaded {count} building records'))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'Error loading building details: {str(e)}'))
                    raise
        
        # Load extra features
        if not skip_features:
            if not os.path.exists(features_file):
                self.stdout.write(self.style.WARNING(
                    f'Features file not found: {features_file}\n'
                    f'Make sure Real_building_land.zip has been downloaded and extracted.'
                ))
            else:
                self.stdout.write(self.style.SUCCESS(f'Loading extra features from {features_file}...'))
                try:
                    count = load_extra_features(features_file)
                    self.stdout.write(self.style.SUCCESS(f'Successfully loaded {count} extra feature records'))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'Error loading extra features: {str(e)}'))
                    raise
        
        self.stdout.write(self.style.SUCCESS('\nImport complete!'))
        self.stdout.write(self.style.SUCCESS('Building details and extra features are now available for similarity searches.'))
