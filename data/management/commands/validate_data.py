"""Management command to validate database integrity on live data.

Checks for duplicates, data completeness, and foreign key integrity
across PropertyRecord, BuildingDetail, and ExtraFeature tables.

Usage:
    python manage.py validate_data
    python manage.py validate_data --verbose
"""

from __future__ import annotations

import sys
from typing import List, Tuple

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from data.models import BuildingDetail, ExtraFeature, PropertyRecord


class Command(BaseCommand):
    """Validate database integrity on the live dataset."""

    help = "Run data quality checks against the live database and report issues."

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output for each check.",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[override]
        verbose: bool = options["verbose"]
        failures: List[Tuple[str, str]] = []

        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("  DATABASE INTEGRITY VALIDATION"))
        self.stdout.write(self.style.SUCCESS("=" * 70))

        # ------------------------------------------------------------------
        # 1. Table counts
        # ------------------------------------------------------------------
        prop_count = PropertyRecord.objects.count()
        building_count = BuildingDetail.objects.filter(is_active=True).count()
        feature_count = ExtraFeature.objects.filter(is_active=True).count()

        self.stdout.write(f"\n  Properties : {prop_count:>10,}")
        self.stdout.write(f"  Buildings  : {building_count:>10,}")
        self.stdout.write(f"  Features   : {feature_count:>10,}")

        if prop_count == 0:
            failures.append(("EMPTY", "PropertyRecord table is empty"))
        if building_count == 0:
            failures.append(("EMPTY", "BuildingDetail table has no active rows"))

        # ------------------------------------------------------------------
        # 2. Duplicate PropertyRecords
        # ------------------------------------------------------------------
        self._section("Duplicate PropertyRecords (by account_number)")
        dup_props = (
            PropertyRecord.objects.values("account_number")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
        )
        dup_count = dup_props.count()
        if dup_count == 0:
            self._pass("No duplicate account_numbers found")
        else:
            msg = f"{dup_count:,} account_numbers have duplicates"
            self._fail(msg)
            failures.append(("DUPLICATE", msg))
            if verbose:
                for d in dup_props[:10]:
                    self.stdout.write(
                        f"    {d['account_number']}: {d['cnt']} records"
                    )

        # ------------------------------------------------------------------
        # 3. Duplicate BuildingDetails
        # ------------------------------------------------------------------
        self._section("Duplicate BuildingDetails (by account + building_number)")
        dup_buildings = (
            BuildingDetail.objects.filter(is_active=True)
            .values("account_number", "building_number")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
        )
        dup_bld_count = dup_buildings.count()
        if dup_bld_count == 0:
            self._pass("No duplicate buildings found")
        else:
            msg = f"{dup_bld_count:,} (account, building_number) pairs have duplicates"
            self._fail(msg)
            failures.append(("DUPLICATE", msg))

        # ------------------------------------------------------------------
        # 4. Duplicate ExtraFeatures
        # ------------------------------------------------------------------
        self._section("Duplicate ExtraFeatures (by account + code + number)")
        dup_feats = (
            ExtraFeature.objects.filter(is_active=True)
            .values("account_number", "feature_code", "feature_number")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
        )
        dup_feat_count = dup_feats.count()
        if dup_feat_count == 0:
            self._pass("No duplicate features found")
        else:
            msg = f"{dup_feat_count:,} feature combos have duplicates"
            self._fail(msg)
            failures.append(("DUPLICATE", msg))

        # ------------------------------------------------------------------
        # 5. BuildingDetail completeness
        # ------------------------------------------------------------------
        self._section("BuildingDetail completeness (active records)")

        if building_count > 0:
            missing_beds = BuildingDetail.objects.filter(
                is_active=True, bedrooms__isnull=True
            ).count()
            missing_baths = BuildingDetail.objects.filter(
                is_active=True, bathrooms__isnull=True
            ).count()
            missing_quality = BuildingDetail.objects.filter(
                is_active=True, quality_code=""
            ).count()
            missing_heat = BuildingDetail.objects.filter(
                is_active=True, heat_area__isnull=True
            ).count()

            bed_pct = (1 - missing_beds / building_count) * 100
            bath_pct = (1 - missing_baths / building_count) * 100
            quality_pct = (1 - missing_quality / building_count) * 100
            heat_pct = (1 - missing_heat / building_count) * 100

            for label, pct, missing in [
                ("Bedrooms", bed_pct, missing_beds),
                ("Bathrooms", bath_pct, missing_baths),
                ("Quality Code", quality_pct, missing_quality),
                ("Heat Area", heat_pct, missing_heat),
            ]:
                if pct >= 90.0:
                    self._pass(f"{label}: {pct:.1f}% populated ({missing:,} missing)")
                elif pct >= 50.0:
                    self._warn(f"{label}: {pct:.1f}% populated ({missing:,} missing)")
                else:
                    msg = f"{label}: only {pct:.1f}% populated ({missing:,} missing)"
                    self._fail(msg)
                    failures.append(("COMPLETENESS", msg))

        # ------------------------------------------------------------------
        # 6. Orphaned BuildingDetails (no matching PropertyRecord)
        # ------------------------------------------------------------------
        self._section("Orphaned records (FK integrity)")

        orphan_buildings = BuildingDetail.objects.filter(
            is_active=True, property__isnull=True
        ).count()

        orphan_features = ExtraFeature.objects.filter(
            is_active=True, property__isnull=True
        ).count()

        if orphan_buildings == 0:
            self._pass("No orphaned BuildingDetail records")
        else:
            msg = f"{orphan_buildings:,} BuildingDetails have no matching PropertyRecord"
            self._warn(msg)

        if orphan_features == 0:
            self._pass("No orphaned ExtraFeature records")
        else:
            msg = f"{orphan_features:,} ExtraFeatures have no matching PropertyRecord"
            self._warn(msg)

        # ------------------------------------------------------------------
        # 7. GIS coverage
        # ------------------------------------------------------------------
        self._section("GIS coordinate coverage")

        if prop_count > 0:
            with_coords = PropertyRecord.objects.filter(
                latitude__isnull=False, longitude__isnull=False
            ).count()
            coord_pct = with_coords / prop_count * 100

            if coord_pct >= 80.0:
                self._pass(f"{coord_pct:.1f}% of properties have coordinates ({with_coords:,}/{prop_count:,})")
            elif coord_pct >= 30.0:
                self._warn(f"{coord_pct:.1f}% of properties have coordinates ({with_coords:,}/{prop_count:,})")
            else:
                msg = f"Only {coord_pct:.1f}% of properties have coordinates"
                self._fail(msg)
                failures.append(("GIS", msg))

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        self.stdout.write("\n" + "=" * 70)
        if failures:
            self.stdout.write(self.style.ERROR(f"  VALIDATION FAILED — {len(failures)} issue(s)"))
            for category, msg in failures:
                self.stdout.write(self.style.ERROR(f"    [{category}] {msg}"))
            self.stdout.write("=" * 70 + "\n")
            sys.exit(1)
        else:
            self.stdout.write(self.style.SUCCESS("  ✅ ALL CHECKS PASSED"))
            self.stdout.write("=" * 70 + "\n")

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------
    def _section(self, title: str) -> None:
        self.stdout.write(f"\n  [{title}]")

    def _pass(self, msg: str) -> None:
        self.stdout.write(self.style.SUCCESS(f"    ✓ {msg}"))

    def _fail(self, msg: str) -> None:
        self.stdout.write(self.style.ERROR(f"    ✗ {msg}"))

    def _warn(self, msg: str) -> None:
        self.stdout.write(self.style.WARNING(f"    ⚠ {msg}"))
