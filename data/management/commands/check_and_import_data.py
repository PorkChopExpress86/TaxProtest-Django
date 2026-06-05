import logging
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand

from data.models import BuildingDetail, PropertyRecord

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Checks if all required data exists and triggers full import if missing."

    def handle(self, *args, **options):
        self.stdout.write("Checking data integrity...")

        # Check counts
        prop_count = PropertyRecord.objects.count()
        building_count = BuildingDetail.objects.count()
        # GIS check: check if we have a reasonable percentage of coordinates
        total_props = prop_count if prop_count > 0 else 1
        coords_count = PropertyRecord.objects.filter(latitude__isnull=False).count()
        coord_coverage = coords_count / total_props

        missing_data = []
        if prop_count == 0:
            missing_data.append("Properties")
        if building_count == 0:
            missing_data.append("Buildings")
        if coord_coverage < 0.1:  # Threshold: at least 10% should have coords to count as "present"
            missing_data.append("GIS Data")

        if missing_data:
            self.stdout.write(
                self.style.WARNING(
                    f"Missing data detected: {', '.join(missing_data)}. Checking for local files..."
                )
            )

            # Check if we have the files locally (e.g. baked into image)
            # We assume if Real_acct_owner exists, we are good to go.
            from django.conf import settings

            extract_dir = Path(settings.HCAD_EXTRACT_DIR)
            has_files = (
                (extract_dir / "Real_acct_owner").exists()
                and (extract_dir / "Real_building_land").exists()
                and (extract_dir / "Parcels").exists()
            )

            import_options = {}
            if has_files:
                self.stdout.write(
                    self.style.SUCCESS("Local extracted files found. Skipping download & extract.")
                )
                # has_files verifies the extracted directories exist, so we can
                # skip both download and extraction and load them directly.
                import_options["skip_download"] = True
                import_options["skip_extract"] = True
            else:
                self.stdout.write(
                    self.style.WARNING("Local data files NOT found. Will attempt download.")
                )

            try:
                call_command("import_all_data", **import_options)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Automatic import failed: {e}"))
                import sys

                sys.exit(1)
        else:
            self.stdout.write(self.style.SUCCESS("Data verification passed:"))
            self.stdout.write(f"  Properties: {prop_count:,}")
            self.stdout.write(f"  Buildings:  {building_count:,}")
            self.stdout.write(f"  GIS Coords: {coords_count:,} ({coord_coverage:.1%})")
