"""Management command to validate database integrity on live data.

Checks for duplicates, data completeness, and foreign key integrity
across PropertyRecord, BuildingDetail, and ExtraFeature tables.

Usage:
    python manage.py validate_data
    python manage.py validate_data --verbose
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from data.models import BuildingDetail, ExtraFeature, PropertyRecord


def _env_float(name: str, default: float) -> float:
    """Read a percentage threshold from the environment, falling back to default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Completeness tolerances. Real HCAD data never reaches exactly 100% — a small
# fraction of residential accounts legitimately lack a building record, fixture
# data, or a parcel geometry. These thresholds pass on healthy data and only
# hard-fail when a metric falls past the floor, signalling a broken load.
# All are percentages and overridable via environment variables.
MIN_GIS_COVERAGE_PCT = _env_float("VALIDATE_MIN_GIS_COVERAGE_PCT", 99.0)
MAX_MISSING_BUILDING_PCT = _env_float("VALIDATE_MAX_MISSING_BUILDING_PCT", 1.0)
MAX_MISSING_ROOM_PCT = _env_float("VALIDATE_MAX_MISSING_ROOM_PCT", 1.0)
MAX_NOT_READY_PCT = _env_float("VALIDATE_MAX_NOT_READY_PCT", 1.0)


class Command(BaseCommand):
    """Validate database integrity on the live dataset."""

    help = "Run data quality checks against the live database and report issues."

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output for each check.",
        )
        parser.add_argument(
            "--skip-building-checks",
            action="store_true",
            help="Skip active-building and room-count completeness checks.",
        )
        parser.add_argument(
            "--skip-gis-checks",
            action="store_true",
            help="Skip GIS coordinate completeness checks.",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[override]
        verbose: bool = options["verbose"]
        skip_building_checks: bool = options["skip_building_checks"]
        skip_gis_checks: bool = options["skip_gis_checks"]
        failures: list[tuple[str, str]] = []

        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("  DATABASE INTEGRITY VALIDATION"))
        self.stdout.write(self.style.SUCCESS("=" * 70))

        # ------------------------------------------------------------------
        # 1. Table counts
        # ------------------------------------------------------------------
        prop_count = PropertyRecord.objects.count()
        residential_prop_count = PropertyRecord.objects.filter(is_residential=True).count()
        ready_prop_count = PropertyRecord.objects.filter(is_data_ready=True).count()
        building_count = BuildingDetail.objects.filter(is_active=True).count()
        feature_count = ExtraFeature.objects.filter(is_active=True).count()

        self.stdout.write(f"\n  Properties : {prop_count:>10,}")
        self.stdout.write(f"  Residential: {residential_prop_count:>10,}")
        self.stdout.write(f"  Ready      : {ready_prop_count:>10,}")
        self.stdout.write(f"  Buildings  : {building_count:>10,}")
        self.stdout.write(f"  Features   : {feature_count:>10,}")

        if prop_count == 0:
            failures.append(("EMPTY", "PropertyRecord table is empty"))
        if building_count == 0 and not skip_building_checks:
            failures.append(("EMPTY", "BuildingDetail table has no active rows"))

        # ------------------------------------------------------------------
        # 2. Residential-only readiness contract
        # ------------------------------------------------------------------
        self._section("PropertyRecord residential readiness")

        if prop_count > 0:
            missing_state_class = PropertyRecord.objects.filter(state_class="").count()
            non_residential = PropertyRecord.objects.filter(is_residential=False).count()
            not_ready = PropertyRecord.objects.filter(
                is_residential=True,
                is_data_ready=False,
            ).count()
            residential_without_buildings = (
                PropertyRecord.objects.filter(is_residential=True)
                .exclude(buildings__is_active=True)
                .distinct()
                .count()
            )
            residential_missing_room_data = (
                PropertyRecord.objects.filter(is_residential=True)
                .exclude(
                    buildings__is_active=True,
                    buildings__bedrooms__isnull=False,
                    buildings__bathrooms__isnull=False,
                )
                .distinct()
                .count()
            )
            residential_missing_gis = (
                PropertyRecord.objects.filter(
                    is_residential=True,
                )
                .filter(Q(latitude__isnull=True) | Q(longitude__isnull=True))
                .count()
            )

            if missing_state_class == 0:
                self._pass("All properties have an HCAD state class")
            else:
                msg = f"{missing_state_class:,} properties are missing state_class"
                self._fail(msg)
                failures.append(("CLASSIFICATION", msg))

            if non_residential == 0:
                self._pass("PropertyRecord contains residential-only rows")
            else:
                msg = f"{non_residential:,} non-residential properties remain in PropertyRecord"
                self._fail(msg)
                failures.append(("RESIDENTIAL", msg))

            if skip_building_checks:
                self._warn("Skipped active-building and room-count readiness checks")
            else:
                self._check_missing(
                    failures,
                    category="BUILDINGS",
                    missing=residential_without_buildings,
                    total=residential_prop_count,
                    max_pct=MAX_MISSING_BUILDING_PCT,
                    pass_msg="All residential properties have an active building record",
                    metric="residential properties have no active building",
                )
                self._check_missing(
                    failures,
                    category="ROOMS",
                    missing=residential_missing_room_data,
                    total=residential_prop_count,
                    max_pct=MAX_MISSING_ROOM_PCT,
                    pass_msg="All residential properties have populated bedroom/bathroom counts",
                    metric="residential properties are missing bedroom/bathroom data",
                )

            if skip_gis_checks:
                self._warn("Skipped GIS coordinate readiness checks")
            else:
                self._check_missing(
                    failures,
                    category="GIS",
                    missing=residential_missing_gis,
                    total=residential_prop_count,
                    max_pct=MAX_MISSING_BUILDING_PCT,  # share the building tolerance
                    pass_msg="All residential properties have GIS coordinates",
                    metric="residential properties are missing GIS coordinates",
                )

            if skip_building_checks or skip_gis_checks:
                readiness_msg = f"{ready_prop_count:,}/{residential_prop_count:,} residential properties are currently marked data-ready"
                if not_ready == 0:
                    self._pass(readiness_msg)
                else:
                    self._warn(readiness_msg)
            else:
                self._check_missing(
                    failures,
                    category="READINESS",
                    missing=not_ready,
                    total=residential_prop_count,
                    max_pct=MAX_NOT_READY_PCT,
                    pass_msg="All residential properties are marked data-ready",
                    metric="residential properties are not data-ready",
                )

        # ------------------------------------------------------------------
        # 3. Duplicate PropertyRecords
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
                    self.stdout.write(f"    {d['account_number']}: {d['cnt']} records")

        # ------------------------------------------------------------------
        # 4. Duplicate BuildingDetails
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
        # 5. Duplicate ExtraFeatures
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
        # 6. BuildingDetail completeness
        # ------------------------------------------------------------------
        self._section("BuildingDetail completeness (active records)")

        residential_buildings = BuildingDetail.objects.filter(
            is_active=True,
            property__is_residential=True,
        )
        residential_building_count = residential_buildings.count()

        if skip_building_checks:
            self._warn("Skipped building completeness percentages by request")
        elif residential_building_count > 0:
            missing_beds = residential_buildings.filter(bedrooms__isnull=True).count()
            missing_baths = residential_buildings.filter(bathrooms__isnull=True).count()
            missing_quality = residential_buildings.filter(quality_code="").count()
            missing_heat = residential_buildings.filter(heat_area__isnull=True).count()

            bed_pct = (1 - missing_beds / residential_building_count) * 100
            bath_pct = (1 - missing_baths / residential_building_count) * 100
            quality_pct = (1 - missing_quality / residential_building_count) * 100
            heat_pct = (1 - missing_heat / residential_building_count) * 100

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
        # 7. Orphaned BuildingDetails (no matching PropertyRecord)
        # ------------------------------------------------------------------
        self._section("Orphaned records (FK integrity)")

        orphan_buildings = BuildingDetail.objects.filter(
            is_active=True, property__isnull=True
        ).count()

        orphan_features = ExtraFeature.objects.filter(is_active=True, property__isnull=True).count()

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
        # 8. GIS coverage
        # ------------------------------------------------------------------
        self._section("GIS coordinate coverage")

        if skip_gis_checks:
            self._warn("Skipped GIS coverage check by request")
        elif residential_prop_count > 0:
            with_coords = PropertyRecord.objects.filter(
                is_residential=True, latitude__isnull=False, longitude__isnull=False
            ).count()
            coord_pct = with_coords / residential_prop_count * 100
            coverage_msg = (
                f"{coord_pct:.1f}% of residential properties have coordinates "
                f"({with_coords:,}/{residential_prop_count:,})"
            )

            if coord_pct >= 100.0:
                self._pass(coverage_msg)
            elif coord_pct >= MIN_GIS_COVERAGE_PCT:
                self._warn(
                    f"{coverage_msg} — within tolerance (>= {MIN_GIS_COVERAGE_PCT:.1f}%)"
                )
            else:
                msg = (
                    f"Only {coord_pct:.1f}% of residential properties have coordinates "
                    f"(below {MIN_GIS_COVERAGE_PCT:.1f}% floor)"
                )
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
            raise CommandError("; ".join(msg for _, msg in failures))
        else:
            self.stdout.write(self.style.SUCCESS("  ✅ ALL CHECKS PASSED"))
            self.stdout.write("=" * 70 + "\n")

    def _check_missing(
        self,
        failures: list[tuple[str, str]],
        *,
        category: str,
        missing: int,
        total: int,
        max_pct: float,
        pass_msg: str,
        metric: str,
    ) -> None:
        """Tiered completeness check: pass at 0, warn within tolerance, fail past floor.

        ``missing``/``total`` define the incomplete share; it passes outright when
        nothing is missing, warns when the share stays within ``max_pct`` (healthy
        residual in real HCAD data), and only records a hard failure when the share
        exceeds the floor — the signature of a broken or partial load.
        """
        if missing == 0:
            self._pass(pass_msg)
            return

        pct = (missing / total * 100) if total else 0.0
        detail = f"{missing:,} {metric} ({pct:.2f}%)"
        if pct <= max_pct:
            self._warn(f"{detail} — within tolerance (<= {max_pct:.1f}%)")
        else:
            msg = f"{detail} — exceeds {max_pct:.1f}% tolerance"
            self._fail(msg)
            failures.append((category, msg))

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
