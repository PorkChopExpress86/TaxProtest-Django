"""Management command to run complete data import: properties, buildings, and GIS data.

Usage:
    python manage.py import_all_data [--skip-download] [--skip-property] [--skip-building] [--skip-gis]
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from data.tasks_new import download_and_extract_hcad


class Command(BaseCommand):
    help = 'Run complete data import: download, property records, building details, and GIS coordinates'

    def run_stage_command(self, command_name: str, **kwargs) -> None:
        """Run a child management command."""
        call_command(command_name, **kwargs)

    def validate_import_contract(self, *, skip_building_checks: bool, skip_gis_checks: bool) -> None:
        """Validate the requested post-import completeness contract."""
        call_command(
            'validate_data',
            skip_building_checks=skip_building_checks,
            skip_gis_checks=skip_gis_checks,
        )

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-download',
            action='store_true',
            help='Skip downloading files (use existing)',
        )
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

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('COMPLETE DATA IMPORT'))
        self.stdout.write(self.style.SUCCESS('=' * 70))

        if not options['skip_download']:
            self.stdout.write(self.style.SUCCESS('\n[0/3] Downloading HCAD data...'))
            self.stdout.write('-' * 70)
            try:
                download_and_extract_hcad.run()
                self.stdout.write(self.style.SUCCESS('✓ Download and extraction complete.'))
            except Exception as e:
                raise CommandError(f'Download failed: {e}') from e
        else:
            self.stdout.write(self.style.WARNING('\n[0/3] Skipping download (using existing files)\n'))

        if not options['skip_property']:
            self.stdout.write(self.style.SUCCESS('\n[1/3] Importing property records...'))
            self.stdout.write('-' * 70)
            try:
                self.run_stage_command('load_hcad_real_acct')
                self.stdout.write(self.style.SUCCESS('✓ Property records imported successfully\n'))
            except Exception as e:
                raise CommandError(f'Property import failed: {e}') from e
        else:
            self.stdout.write(self.style.WARNING('\n[1/3] Skipping property records import\n'))

        if not options['skip_building']:
            self.stdout.write(self.style.SUCCESS('[2/3] Importing building data...'))
            self.stdout.write('-' * 70)
            try:
                self.run_stage_command('import_building_data', skip_download=True)
                self.stdout.write(self.style.SUCCESS('✓ Building data imported successfully\n'))
            except Exception as e:
                raise CommandError(f'Building import failed: {e}') from e
        else:
            self.stdout.write(self.style.WARNING('[2/3] Skipping building data import\n'))

        if not options['skip_gis']:
            self.stdout.write(self.style.SUCCESS('[3/3] Importing GIS coordinates...'))
            self.stdout.write('-' * 70)
            try:
                self.run_stage_command('load_gis_data', skip_download=True)
                self.stdout.write(self.style.SUCCESS('✓ GIS data imported successfully\n'))
            except Exception as e:
                raise CommandError(f'GIS import failed: {e}') from e
        else:
            self.stdout.write(self.style.WARNING('[3/3] Skipping GIS data import\n'))

        self.stdout.write(self.style.SUCCESS('[validation] Verifying residential-ready completeness...'))
        self.stdout.write('-' * 70)

        try:
            self.validate_import_contract(
                skip_building_checks=options['skip_building'],
                skip_gis_checks=options['skip_gis'],
            )
            self.stdout.write(self.style.SUCCESS('✓ Completeness validation passed\n'))
        except CommandError as e:
            self.stdout.write(self.style.ERROR(f'✗ Completeness validation failed: {e}\n'))
            raise

        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('IMPORT COMPLETE'))
        self.stdout.write(self.style.SUCCESS('=' * 70))

        from data.models import BuildingDetail, ExtraFeature, PropertyRecord

        total_props = PropertyRecord.objects.count()
        residential_props = PropertyRecord.objects.filter(is_residential=True).count()
        ready_props = PropertyRecord.objects.filter(is_data_ready=True).count()
        total_buildings = BuildingDetail.objects.filter(is_active=True).count()
        total_features = ExtraFeature.objects.filter(is_active=True).count()
        props_with_coords = PropertyRecord.objects.filter(
            is_residential=True,
            latitude__isnull=False,
            longitude__isnull=False,
        ).count()

        self.stdout.write('\nDatabase Statistics:')
        self.stdout.write(f'  Properties:        {total_props:>10,}')
        self.stdout.write(f'  Residential:       {residential_props:>10,}')
        self.stdout.write(f'  Ready:             {ready_props:>10,}')
        self.stdout.write(f'  Buildings:         {total_buildings:>10,}')
        self.stdout.write(f'  Features:          {total_features:>10,}')
        if residential_props > 0:
            self.stdout.write(
                f'  With coordinates:  {props_with_coords:>10,} '
                f'({props_with_coords / residential_props * 100:.1f}% of residential)'
            )
        else:
            self.stdout.write(f'  With coordinates:  {props_with_coords:>10,} (0.0%)')

        self.stdout.write('\n' + '=' * 70 + '\n')
        self.stdout.write(self.style.SUCCESS('IMPORT COMPLETE - EXITING'))
