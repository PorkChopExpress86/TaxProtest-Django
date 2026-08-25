"""Load GIS parcel coordinates through the authoritative Harris pipeline."""

from dataclasses import replace
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from counties.harris.etl_pipeline import DownloadManager, ETLConfig, ExtractManager
from counties.harris.etl_pipeline.gis_loader import load_gis_parcels, select_preferred_gis_shapefile
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId


def find_preferred_shapefile(extract_dir: str) -> str | None:
    """Compatibility adapter for the shared GIS-layer selection rule."""
    selected = select_preferred_gis_shapefile([Path(extract_dir)])
    return str(selected) if selected is not None else None


class Command(BaseCommand):
    help = "Download and load GIS parcel data from HCAD"

    def add_arguments(self, parser):
        default_source = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.GIS_PARCELS)
        parser.add_argument(
            "--url",
            type=str,
            default=default_source.url_template,
            help="URL to download GIS Parcels.zip from",
        )
        parser.add_argument(
            "--skip-download",
            action="store_true",
            help="Skip download and use existing extracted files",
        )
        parser.add_argument(
            "--no-refresh-readiness",
            action="store_true",
            help="Skip readiness recomputation after GIS import",
        )

    def handle(self, *args, **options):
        config = ETLConfig.from_env()
        source = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.GIS_PARCELS)
        if options["url"] != source.url_template:
            source = replace(source, url_template=options["url"])

        download_manager = DownloadManager(config)
        extract_manager = ExtractManager(config)
        if not options["skip_download"]:
            self.stdout.write(
                self.style.SUCCESS(f"Downloading GIS data from {source.url_template}...")
            )
            download_result = download_manager.download_file(source)
            if not download_result.success or download_result.local_path is None:
                raise CommandError(f"GIS download failed: {download_result.error}")

            self.stdout.write(self.style.SUCCESS(f"Extracting {source.filename}..."))
            extract_result = extract_manager.extract_archive(source, download_result.local_path)
            if not extract_result.success:
                raise CommandError(f"GIS extraction failed: {extract_result.error}")
            self.stdout.write(self.style.SUCCESS(f"Extracted to {extract_result.extract_dir}"))
        else:
            self.stdout.write("Skipping download and extraction, using existing files...")

        extract_dir = extract_manager.get_extract_path(source)
        legacy_extract_dir = config.download_dir / source.filename.rsplit(".", 1)[0]
        shapefile = select_preferred_gis_shapefile([extract_dir, legacy_extract_dir])

        if shapefile is None:
            self.stdout.write(self.style.ERROR(f"No shapefile (.shp) found in {extract_dir}"))
            return

        shapefile_path = str(shapefile)
        self.stdout.write(self.style.SUCCESS(f"Found shapefile: {shapefile_path}"))
        self.stdout.write(self.style.SUCCESS("Loading GIS data into database..."))

        # Load the GIS data
        try:
            count = load_gis_parcels(
                shapefile_path,
                refresh_readiness=not options.get("no_refresh_readiness", False),
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully updated {count} property records with GIS coordinates"
                )
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error loading GIS data: {str(e)}"))
            raise
