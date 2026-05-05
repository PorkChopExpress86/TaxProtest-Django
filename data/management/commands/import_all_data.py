"""Management command to run complete data import via the modern ETL pipeline.

Usage:
    python manage.py import_all_data [--skip-download] [--skip-property] [--skip-building] [--skip-gis]
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from data.etl_pipeline import ETLConfig, ETLOrchestrator
from data.etl_pipeline.config import DataSource, DataSourceType


class Command(BaseCommand):
    help = 'Run complete data import via the authoritative modern ETL pipeline'

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-download',
            action='store_true',
            help='Skip downloading and extraction (use existing extracted files)',
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

    @staticmethod
    def _select_sources(config: ETLConfig, *, include_property: bool, include_building: bool, include_gis: bool) -> list[DataSource]:
        selected: list[DataSource] = []
        for source in config.get_required_sources():
            if source.source_type == DataSourceType.GIS_DATA:
                if include_gis:
                    selected.append(source)
                continue

            if source.name == 'Real Account Owner':
                if include_property:
                    selected.append(source)
                continue

            if source.name == 'Real Building Land':
                if include_building:
                    selected.append(source)
                continue

        return selected

    @staticmethod
    def _resolve_scope(*, include_property: bool, include_building: bool, include_gis: bool) -> str:
        if include_property and include_building and include_gis:
            return 'full'
        if include_building and not include_property and not include_gis:
            return 'building-only'
        if include_gis and not include_property and not include_building:
            return 'gis-only'
        if include_property and not include_building and not include_gis:
            return 'property-only'
        if include_property and include_building and not include_gis:
            return 'property-only'
        return 'full'

    def handle(self, *args, **options):
        include_property = not options['skip_property']
        include_building = not options['skip_building']
        include_gis = not options['skip_gis']

        if not any([include_property, include_building, include_gis]):
            raise CommandError('Nothing to import: all import stages were skipped.')

        config = ETLConfig.from_env()
        sources = self._select_sources(
            config,
            include_property=include_property,
            include_building=include_building,
            include_gis=include_gis,
        )

        if not sources:
            raise CommandError('No required modern ETL sources matched the selected import stages.')

        scope = self._resolve_scope(
            include_property=include_property,
            include_building=include_building,
            include_gis=include_gis,
        )

        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('COMPLETE DATA IMPORT (MODERN ETL)'))
        self.stdout.write(self.style.SUCCESS('=' * 70))

        orchestrator = ETLOrchestrator(config)
        result = orchestrator.execute(
            sources=sources,
            scope=scope,
            strict=True,
            validate_contract=True,
            skip_download=options['skip_download'],
            skip_extract=options['skip_download'],
            skip_load=False,
        )

        self.stdout.write('')
        self.stdout.write(self.style.WARNING('Pipeline Results:'))
        self.stdout.write(f'  Status: {result.status.value}')
        self.stdout.write(f'  Duration: {result.duration:.1f}s')

        for stage, stage_result in result.stages.items():
            status = self.style.SUCCESS('✓') if stage_result.success else self.style.ERROR('✗')
            self.stdout.write(f'  {status} {stage.value}: {stage_result.duration:.1f}s')
            if stage_result.error:
                self.stdout.write(self.style.ERROR(f'      Error: {stage_result.error}'))

        if result.errors:
            self.stdout.write('')
            self.stdout.write(self.style.ERROR('Errors:'))
            for error in result.errors[:10]:
                self.stdout.write(self.style.ERROR(f'  - {error}'))

        if not result.success:
            raise CommandError('Authoritative modern ETL import failed')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Authoritative modern ETL import completed successfully.'))
