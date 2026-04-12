"""Management command to reconcile legacy mixed or incomplete property rows.

Usage:
    python manage.py reconcile_property_data          # dry run
    python manage.py reconcile_property_data --apply  # perform cleanup
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Exists, OuterRef

from data.etl import link_orphaned_records, refresh_property_readiness
from data.models import BuildingDetail, ExtraFeature, PropertyRecord
from data.residential import is_residential_state_class, normalize_state_class


class Command(BaseCommand):
    help = (
        "Dry-run or apply cleanup for legacy non-residential and incomplete "
        "PropertyRecord rows."
    )

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete non-residential and not-ready residential rows instead of only previewing them.",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=5000,
            help="Batch size for previewing and synchronizing classification flags.",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[override]
        apply_changes: bool = options["apply"]
        chunk_size: int = options["chunk_size"]

        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("LEGACY PROPERTY RECONCILIATION"))
        self.stdout.write(self.style.SUCCESS("=" * 70))

        preview = self._preview_cleanup(chunk_size=chunk_size)

        self.stdout.write(f"\nProperties examined:                 {preview['properties_examined']:>10,}")
        self.stdout.write(f"State classes needing normalization: {preview['state_classes_normalized']:>10,}")
        self.stdout.write(f"Residential flags out of sync:       {preview['residential_flags_synced']:>10,}")
        self.stdout.write(f"Ready properties after sync:         {preview['effective_ready_properties']:>10,}")
        self.stdout.write(f"Non-residential rows to remove:      {preview['non_residential_properties']:>10,}")
        self.stdout.write(f"Incomplete residential rows to remove:{preview['incomplete_properties']:>9,}")
        self.stdout.write(f"Rows missing state_class:            {preview['missing_state_class']:>10,}")
        self.stdout.write(f"Orphaned buildings currently present:{preview['orphaned_buildings']:>9,}")
        self.stdout.write(f"Orphaned features currently present: {preview['orphaned_features']:>10,}")

        if not apply_changes:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry run only — no rows were changed. Re-run with --apply to "
                    "synchronize classification flags, relink any orphans, and remove "
                    "legacy non-residential or incomplete rows."
                )
            )
            return

        sync_results = self._sync_property_flags(chunk_size=chunk_size)
        link_results = link_orphaned_records(chunk_size=chunk_size)
        readiness_results = refresh_property_readiness()

        non_residential_qs = PropertyRecord.objects.filter(is_residential=False)
        incomplete_qs = PropertyRecord.objects.filter(
            is_residential=True,
            is_data_ready=False,
        )
        orphaned_buildings_qs = BuildingDetail.objects.filter(property__isnull=True)
        orphaned_features_qs = ExtraFeature.objects.filter(property__isnull=True)

        deletion_totals: Counter[str] = Counter()

        with transaction.atomic():
            _, orphan_building_details = orphaned_buildings_qs.delete()
            deletion_totals.update(orphan_building_details)

            _, orphan_feature_details = orphaned_features_qs.delete()
            deletion_totals.update(orphan_feature_details)

            _, non_residential_details = non_residential_qs.delete()
            deletion_totals.update(non_residential_details)

            _, incomplete_details = incomplete_qs.delete()
            deletion_totals.update(incomplete_details)

        refresh_property_readiness()

        remaining_non_residential = PropertyRecord.objects.filter(is_residential=False).count()
        remaining_incomplete = PropertyRecord.objects.filter(
            is_residential=True,
            is_data_ready=False,
        ).count()

        self.stdout.write(self.style.SUCCESS("\nSynchronization and cleanup complete."))
        self.stdout.write(f"  State classes normalized:          {sync_results['state_classes_normalized']:>10,}")
        self.stdout.write(f"  Residential flags synchronized:    {sync_results['residential_flags_synced']:>10,}")
        self.stdout.write(f"  Buildings relinked:                {link_results['buildings_linked']:>10,}")
        self.stdout.write(f"  Features relinked:                 {link_results['features_linked']:>10,}")
        self.stdout.write(f"  Ready properties recomputed:       {readiness_results['ready_properties_set']:>10,}")
        self.stdout.write(f"  Properties deleted:                {deletion_totals.get('data.PropertyRecord', 0):>10,}")
        self.stdout.write(f"  Buildings deleted:                 {deletion_totals.get('data.BuildingDetail', 0):>10,}")
        self.stdout.write(f"  Features deleted:                  {deletion_totals.get('data.ExtraFeature', 0):>10,}")

        if remaining_non_residential or remaining_incomplete:
            raise CommandError(
                "Reconciliation left legacy rows behind: "
                f"{remaining_non_residential:,} non-residential and "
                f"{remaining_incomplete:,} incomplete residential properties remain."
            )

        self.stdout.write(self.style.SUCCESS("\nAll remaining PropertyRecord rows are residential and data-ready."))

    def _preview_cleanup(self, *, chunk_size: int) -> dict[str, int]:
        ready_buildings = BuildingDetail.objects.filter(
            property_id=OuterRef("pk"),
            is_active=True,
            bedrooms__isnull=False,
            bathrooms__isnull=False,
        )

        queryset = PropertyRecord.objects.annotate(
            has_ready_building=Exists(ready_buildings)
        ).only("pk", "state_class", "is_residential", "latitude", "longitude")

        results = {
            "properties_examined": 0,
            "state_classes_normalized": 0,
            "residential_flags_synced": 0,
            "effective_ready_properties": 0,
            "non_residential_properties": 0,
            "incomplete_properties": 0,
            "missing_state_class": 0,
            "orphaned_buildings": BuildingDetail.objects.filter(property__isnull=True).count(),
            "orphaned_features": ExtraFeature.objects.filter(property__isnull=True).count(),
        }

        for prop in queryset.iterator(chunk_size=chunk_size):
            normalized_state_class = normalize_state_class(prop.state_class)
            effective_is_residential = is_residential_state_class(normalized_state_class)
            has_coords = prop.latitude is not None and prop.longitude is not None
            effective_is_ready = (
                effective_is_residential
                and bool(getattr(prop, "has_ready_building", False))
                and has_coords
            )

            results["properties_examined"] += 1

            if normalized_state_class != prop.state_class:
                results["state_classes_normalized"] += 1
            if effective_is_residential != prop.is_residential:
                results["residential_flags_synced"] += 1
            if not normalized_state_class:
                results["missing_state_class"] += 1

            if not effective_is_residential:
                results["non_residential_properties"] += 1
            elif effective_is_ready:
                results["effective_ready_properties"] += 1
            else:
                results["incomplete_properties"] += 1

        return results

    def _sync_property_flags(self, *, chunk_size: int) -> dict[str, int]:
        updates = []
        results = {
            "state_classes_normalized": 0,
            "residential_flags_synced": 0,
            "properties_updated": 0,
        }

        queryset = PropertyRecord.objects.only("pk", "state_class", "is_residential")

        for prop in queryset.iterator(chunk_size=chunk_size):
            normalized_state_class = normalize_state_class(prop.state_class)
            effective_is_residential = is_residential_state_class(normalized_state_class)

            state_changed = prop.state_class != normalized_state_class
            flag_changed = prop.is_residential != effective_is_residential

            if not state_changed and not flag_changed:
                continue

            if state_changed:
                results["state_classes_normalized"] += 1
            if flag_changed:
                results["residential_flags_synced"] += 1

            prop.state_class = normalized_state_class
            prop.is_residential = effective_is_residential
            updates.append(prop)

            if len(updates) >= chunk_size:
                PropertyRecord.objects.bulk_update(
                    updates,
                    ["state_class", "is_residential"],
                    batch_size=chunk_size,
                )
                results["properties_updated"] += len(updates)
                updates.clear()

        if updates:
            PropertyRecord.objects.bulk_update(
                updates,
                ["state_class", "is_residential"],
                batch_size=chunk_size,
            )
            results["properties_updated"] += len(updates)

        return results