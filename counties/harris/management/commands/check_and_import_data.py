import logging
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.urls import reverse

from counties.common.import_audit import reconcile_dead_operations
from counties.common.import_states import CandidateState
from counties.common.import_writers import WriterConflict
from counties.common.models import ImportCandidate
from counties.harris.models import BuildingDetail, PropertyRecord
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

logger = logging.getLogger(__name__)

# Extracted sources whose presence lets an automatic import skip download and extraction.
_LOCAL_SOURCES = (
    HcadSourceId.REAL_ACCOUNT_OWNER,
    HcadSourceId.REAL_BUILDING_LAND,
    HcadSourceId.GIS_PARCELS,
)

# Candidate states that may still publish, with what the operator does next.
_NEXT_STEP_FOR_PENDING = {
    CandidateState.PREPARING: "Wait for it to finish, then review it",
    CandidateState.AWAITING_REVIEW: "Review it, then apply it",
    CandidateState.APPROVED: "Apply it",
    CandidateState.PREPARED: "Apply it",
}


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
            # A preparation whose worker died never finishes; reconciling blocks its candidate.
            stuck_writer = None
            try:
                reconcile_dead_operations("harris", actor="check_and_import_data")
            except WriterConflict as conflict:
                if conflict.uncertain:
                    stuck_writer = conflict.operation_id
            # A first import publishes nothing until an operator resolves its candidate,
            # so a restart in the meantime must not start another import.
            pending = ImportCandidate.objects.filter(
                county="harris", state__in=_NEXT_STEP_FOR_PENDING
            ).first()
            if pending is not None:
                link = reverse("admin:data_importcandidate_change", args=[pending.pk])
                next_step = f"{_NEXT_STEP_FOR_PENDING[pending.state]} in Django admin {link}"
                if pending.state == CandidateState.PREPARING and stuck_writer is not None:
                    recovery = reverse(
                        "admin:data_importoperation_recover_writer", args=[stuck_writer]
                    )
                    next_step = (
                        "Its worker stopped without releasing the county writer. Recover the "
                        f"county writer in Django admin {recovery}, then restart to block this "
                        "Candidate and import again"
                    )
                self.stdout.write(
                    self.style.WARNING(
                        f"Harris Candidate {pending.pk} is {pending.state}; published data "
                        f"unchanged. Not starting another import. {next_step}."
                    )
                )
                return

            self.stdout.write(
                self.style.WARNING(
                    f"Missing data detected: {', '.join(missing_data)}. Checking for local files..."
                )
            )

            # Check if we have the files locally (e.g. baked into image)
            from django.conf import settings

            extract_dir = Path(settings.HCAD_EXTRACT_DIR)
            has_files = all(
                (extract_dir / DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(source_id)).exists()
                for source_id in _LOCAL_SOURCES
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
