"""
Management command for ETL pipeline operations.

Usage:
    python manage.py etl_pipeline --help
    python manage.py etl_pipeline download
    python manage.py etl_pipeline extract
    python manage.py etl_pipeline run
    python manage.py etl_pipeline status
"""

import json
from django.core.management.base import BaseCommand, CommandError

from data.etl_pipeline import (
    ETLConfig, ETLOrchestrator, DownloadManager, ExtractManager
)
from data.etl_pipeline.config import DataSourceType


class Command(BaseCommand):
    help = 'Run ETL pipeline operations for HCAD data import'

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest='command', help='ETL command to run')
        
        # Download command
        download_parser = subparsers.add_parser('download', help='Download HCAD data files')
        download_parser.add_argument(
            '--all',
            action='store_true',
            help='Download all sources including optional ones',
        )
        download_parser.add_argument(
            '--source',
            type=str,
            help='Download a specific source by name',
        )
        download_parser.add_argument(
            '--year',
            type=int,
            help='Data year (default: current year)',
        )
        
        # Extract command
        extract_parser = subparsers.add_parser('extract', help='Extract downloaded archives')
        extract_parser.add_argument(
            '--source',
            type=str,
            help='Extract a specific source by name',
        )
        extract_parser.add_argument(
            '--validate',
            action='store_true',
            default=True,
            help='Validate archives before extraction (default: True)',
        )
        
        # Run command (full pipeline)
        run_parser = subparsers.add_parser('run', help='Run the full ETL pipeline')
        run_parser.add_argument(
            '--skip-download',
            action='store_true',
            help='Skip download stage (use existing files)',
        )
        run_parser.add_argument(
            '--skip-extract',
            action='store_true',
            help='Skip extract stage (use existing extracted files)',
        )
        run_parser.add_argument(
            '--skip-load',
            action='store_true',
            help='Skip load stage (dry run for transform)',
        )
        run_parser.add_argument(
            '--year',
            type=int,
            help='Data year (default: current year)',
        )
        run_parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )
        run_parser.add_argument(
            '--property-only',
            action='store_true',
            help='Only process property data (skip GIS)',
        )
        run_parser.add_argument(
            '--gis-only',
            action='store_true',
            help='Only process GIS data (skip property data)',
        )
        
        # Status command
        status_parser = subparsers.add_parser('status', help='Show pipeline status')
        status_parser.add_argument(
            '--json',
            action='store_true',
            help='Output status as JSON',
        )
        
        # Cleanup command
        cleanup_parser = subparsers.add_parser('cleanup', help='Clean up temporary files')
        cleanup_parser.add_argument(
            '--downloads',
            action='store_true',
            help='Remove downloaded ZIP files',
        )
        cleanup_parser.add_argument(
            '--extracts',
            action='store_true',
            default=True,
            help='Remove extracted files (default: True)',
        )
        
        # List command
        list_parser = subparsers.add_parser('list', help='List available data sources')

    def handle(self, *args, **options):
        command = options.get('command')
        
        if not command:
            self.print_help('manage.py', 'etl_pipeline')
            return
        
        # Build configuration
        config = ETLConfig.from_env()
        
        if options.get('year'):
            config.data_year = options['year']
        
        if options.get('dry_run'):
            config.dry_run = True
        
        # Route to appropriate handler
        if command == 'download':
            self.handle_download(config, options)
        elif command == 'extract':
            self.handle_extract(config, options)
        elif command == 'run':
            self.handle_run(config, options)
        elif command == 'status':
            self.handle_status(config, options)
        elif command == 'cleanup':
            self.handle_cleanup(config, options)
        elif command == 'list':
            self.handle_list(config, options)
        else:
            raise CommandError(f"Unknown command: {command}")

    def handle_download(self, config: ETLConfig, options: dict):
        """Handle download command."""
        self.stdout.write(self.style.WARNING(
            f'Starting download (year={config.data_year})...'
        ))
        
        manager = DownloadManager(config)
        
        # Determine which sources to download
        if options.get('source'):
            source = config.get_source_by_name(options['source'])
            if not source:
                raise CommandError(f"Unknown source: {options['source']}")
            sources = [source]
        elif options.get('all'):
            sources = config.get_all_sources()
        else:
            sources = config.get_required_sources()
        
        self.stdout.write(f'Downloading {len(sources)} source(s)...')
        
        results = manager.download_batch(sources)
        
        # Report results
        success = sum(1 for r in results if r.success)
        failed = len(results) - success
        total_bytes = sum(r.bytes_downloaded for r in results)
        
        for result in results:
            if result.success:
                self.stdout.write(self.style.SUCCESS(
                    f'  ✓ {result.source.name}: {result.bytes_downloaded:,} bytes'
                ))
            else:
                self.stdout.write(self.style.ERROR(
                    f'  ✗ {result.source.name}: {result.error}'
                ))
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Download complete: {success}/{len(results)} succeeded, '
            f'{total_bytes:,} bytes total'
        ))
        
        if failed > 0:
            raise CommandError(f'{failed} download(s) failed')

    def handle_extract(self, config: ETLConfig, options: dict):
        """Handle extract command."""
        self.stdout.write(self.style.WARNING('Starting extraction...'))
        
        manager = ExtractManager(config)
        
        # Determine which sources to extract
        if options.get('source'):
            source = config.get_source_by_name(options['source'])
            if not source:
                raise CommandError(f"Unknown source: {options['source']}")
            sources = [source]
        else:
            sources = config.get_all_sources()
        
        # Filter to downloaded sources only
        download_manager = DownloadManager(config)
        sources = [s for s in sources if download_manager.is_downloaded(s)]
        
        if not sources:
            raise CommandError('No downloaded archives found. Run download first.')
        
        self.stdout.write(f'Extracting {len(sources)} archive(s)...')
        
        results = manager.extract_batch(sources)
        
        # Report results
        success = sum(1 for r in results if r.success)
        total_files = sum(len(r.files_extracted) for r in results)
        
        for result in results:
            if result.success:
                self.stdout.write(self.style.SUCCESS(
                    f'  ✓ {result.source.name}: {len(result.files_extracted)} files'
                ))
            else:
                self.stdout.write(self.style.ERROR(
                    f'  ✗ {result.source.name}: {result.error}'
                ))
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Extraction complete: {success}/{len(results)} succeeded, '
            f'{total_files} files extracted'
        ))

    def handle_run(self, config: ETLConfig, options: dict):
        """Handle run command (full pipeline)."""
        self.stdout.write(self.style.WARNING(
            f'Starting ETL pipeline (year={config.data_year}, '
            f'dry_run={config.dry_run})...'
        ))
        
        # Filter sources based on options
        if options.get('property_only'):
            sources = [s for s in config.get_required_sources() 
                      if s.source_type != DataSourceType.GIS_DATA]
        elif options.get('gis_only'):
            sources = [s for s in config.get_all_sources() 
                      if s.source_type == DataSourceType.GIS_DATA]
        else:
            sources = config.get_required_sources()
        
        orchestrator = ETLOrchestrator(config)
        
        result = orchestrator.execute(
            sources=sources,
            skip_download=options.get('skip_download', False),
            skip_extract=options.get('skip_extract', False),
            skip_load=options.get('skip_load', False),
        )
        
        # Report results
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
            for error in result.errors[:5]:
                self.stdout.write(self.style.ERROR(f'  - {error}'))
        
        if not result.success:
            raise CommandError('Pipeline execution failed')
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Pipeline completed successfully!'))

    def handle_status(self, config: ETLConfig, options: dict):
        """Handle status command."""
        download_manager = DownloadManager(config)
        extract_manager = ExtractManager(config)
        
        status = {
            'config': {
                'data_year': config.data_year,
                'download_dir': str(config.download_dir),
                'extract_dir': str(config.extract_dir),
            },
            'sources': [],
        }
        
        for source in config.get_all_sources():
            source_status = {
                'name': source.name,
                'required': source.required,
                'downloaded': download_manager.is_downloaded(source),
                'extracted': extract_manager.is_extracted(source),
            }
            status['sources'].append(source_status)
        
        if options.get('json'):
            self.stdout.write(json.dumps(status, indent=2))
        else:
            self.stdout.write(self.style.WARNING('ETL Pipeline Status'))
            self.stdout.write(f'  Data Year: {config.data_year}')
            self.stdout.write(f'  Download Dir: {config.download_dir}')
            self.stdout.write(f'  Extract Dir: {config.extract_dir}')
            self.stdout.write('')
            self.stdout.write('Sources:')
            
            for s in status['sources']:
                downloaded = self.style.SUCCESS('✓') if s['downloaded'] else self.style.ERROR('✗')
                extracted = self.style.SUCCESS('✓') if s['extracted'] else self.style.ERROR('✗')
                required = '*' if s['required'] else ' '
                self.stdout.write(
                    f'  {required} {s["name"]}: downloaded={downloaded} extracted={extracted}'
                )

    def handle_cleanup(self, config: ETLConfig, options: dict):
        """Handle cleanup command."""
        self.stdout.write(self.style.WARNING('Cleaning up...'))
        
        orchestrator = ETLOrchestrator(config)
        orchestrator.cleanup(
            remove_downloads=options.get('downloads', False),
            remove_extracts=options.get('extracts', True),
        )
        
        self.stdout.write(self.style.SUCCESS('Cleanup complete!'))

    def handle_list(self, config: ETLConfig, options: dict):
        """Handle list command."""
        self.stdout.write(self.style.WARNING('Available Data Sources:'))
        self.stdout.write('')
        
        for source in config.get_all_sources():
            required = self.style.SUCCESS('[required]') if source.required else '[optional]'
            self.stdout.write(f'  {source.name} {required}')
            self.stdout.write(f'    Type: {source.source_type.value}')
            self.stdout.write(f'    URL: {source.get_url(config.data_year)}')
            self.stdout.write(f'    Filename: {source.filename}')
            self.stdout.write('')
