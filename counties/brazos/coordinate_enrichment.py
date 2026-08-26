"""Audited coordinate-only enrichment for an already-certified CAD year.

This is deliberately not an annual refresh.  It permits an earlier BCAD
certified parcel release to fill only latitude/longitude after measuring the
PROP_ID join against a later CAD load.  The actual GIS source year is retained
on every updated account so consumers cannot mistake the result for a
year-matched property snapshot.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.core.management.base import CommandError

from counties.brazos.annual_refresh import RefreshOptions, StagePreparation
from counties.brazos.gis_refresh import GisRefreshStage, GisSourcePayload, normalize_prop_id
from counties.brazos.models import PropertyAccount
from counties.brazos.stage_reporting import SilentStageReporter, StageReporter

COORDINATE_SOURCE = "bcad-certified-gis"
COORDINATE_FIELDS_UPDATED = (
    "latitude",
    "longitude",
    "coordinate_source",
    "coordinate_source_year",
)


@dataclass(frozen=True)
class CoordinateEnrichmentReport:
    """Measured evidence for a single GIS-to-CAD coordinate join."""

    source_year: int
    target_year: int
    source_records: int
    usable_coordinate_records: int
    distinct_source_ids: int
    duplicate_source_ids: int
    invalid_coordinate_records: int
    target_accounts: int
    matched_accounts: int
    unmatched_target_accounts: int
    unmatched_source_ids: int

    @property
    def match_rate(self) -> float:
        return self.matched_accounts / self.target_accounts if self.target_accounts else 0.0


class BrazosCoordinateEnrichment:
    """Measure and, only after an explicit threshold, apply coordinate updates."""

    def __init__(
        self,
        source_stage: GisRefreshStage | None = None,
        reporter: StageReporter | None = None,
    ):
        self._reporter = reporter or SilentStageReporter()
        self._source_stage = source_stage or GisRefreshStage(self._reporter)

    @property
    def stdout(self):
        return self._reporter.stdout

    @staticmethod
    def _coordinate_candidates(
        shapefile_path: Path,
    ) -> tuple[dict[str, tuple[Decimal, Decimal]], dict[str, int]]:
        import geopandas as gpd

        gdf = gpd.read_file(shapefile_path)
        if "PROP_ID" not in gdf:
            raise CommandError(f"BCAD GIS source {shapefile_path} has no PROP_ID column.")
        if gdf.crs is None:
            raise CommandError(
                f"BCAD GIS source {shapefile_path} has no coordinate reference system."
            )
        centroids = gdf.geometry.centroid
        if gdf.crs.to_epsg() != 4326:
            centroids = centroids.to_crs(epsg=4326)

        ids: list[str] = []
        candidates: dict[str, tuple[Decimal, Decimal]] = {}
        invalid_coordinate_records = 0
        for raw_prop_id, point in zip(gdf.get("PROP_ID", []), centroids, strict=True):
            prop_id = normalize_prop_id(raw_prop_id)
            if prop_id is None:
                continue
            ids.append(prop_id)
            if point is None or point.is_empty:
                invalid_coordinate_records += 1
                continue
            try:
                latitude = Decimal(str(point.y))
                longitude = Decimal(str(point.x))
            except (ArithmeticError, ValueError):
                invalid_coordinate_records += 1
                continue
            if not latitude.is_finite() or not longitude.is_finite():
                invalid_coordinate_records += 1
                continue
            candidates[prop_id] = (latitude, longitude)

        counts = Counter(ids)
        return candidates, {
            "source_records": len(gdf),
            "usable_coordinate_records": len(candidates),
            "distinct_source_ids": len(counts),
            "duplicate_source_ids": sum(1 for count in counts.values() if count > 1),
            "invalid_coordinate_records": invalid_coordinate_records,
        }

    def analyze(
        self, options: RefreshOptions
    ) -> tuple[StagePreparation, CoordinateEnrichmentReport, dict[str, tuple[Decimal, Decimal]]]:
        """Stage BCAD GIS and measure its normalized ID coverage without DB writes."""
        if options.tax_year is None:
            raise CommandError("--year is required for coordinate-only enrichment.")

        preparation = self._source_stage.prepare(options)
        if preparation.source_year >= preparation.target_year:
            raise CommandError(
                "Coordinate-only enrichment requires an earlier GIS source year than "
                f"the target CAD year; received GIS {preparation.source_year} and CAD "
                f"{preparation.target_year}. Use refresh_brazos_annual for a year-matched snapshot."
            )
        payload = preparation.payload
        if not isinstance(payload, GisSourcePayload) or payload.shapefile_path is None:
            raise CommandError("No BCAD GIS shapefile was prepared for coordinate analysis.")

        self.stdout.write(f"Reading {payload.shapefile_path} for coordinate coverage ...")
        candidates, source_metrics = self._coordinate_candidates(payload.shapefile_path)
        accounts_by_prop_id = {
            account.prop_id: account
            for account in PropertyAccount.objects.filter(tax_year=preparation.target_year)
        }
        matched_ids = candidates.keys() & accounts_by_prop_id.keys()
        report = CoordinateEnrichmentReport(
            source_year=preparation.source_year,
            target_year=preparation.target_year,
            target_accounts=len(accounts_by_prop_id),
            matched_accounts=len(matched_ids),
            unmatched_target_accounts=len(accounts_by_prop_id) - len(matched_ids),
            unmatched_source_ids=len(candidates) - len(matched_ids),
            **source_metrics,
        )
        return preparation, report, candidates

    def cleanup(self, preparation: StagePreparation) -> None:
        """Remove staged GIS data after a successful, explicitly applied update."""
        self._source_stage.cleanup(preparation)

    @staticmethod
    def _validate_apply(
        report: CoordinateEnrichmentReport, minimum_match_rate: float | None
    ) -> None:
        if minimum_match_rate is None:
            raise CommandError("--apply requires an explicit --minimum-match-rate.")
        if not 0 <= minimum_match_rate <= 1:
            raise CommandError("--minimum-match-rate must be between 0 and 1.")
        if not report.target_accounts:
            raise CommandError(
                "Refusing to apply: the target CAD year has no PropertyAccount rows."
            )
        if not report.usable_coordinate_records:
            raise CommandError("Refusing to apply: the GIS source has no usable coordinates.")
        if report.duplicate_source_ids:
            raise CommandError(
                f"Refusing to apply: {report.duplicate_source_ids} normalized source PROP_IDs "
                "are duplicated. Resolve the source ambiguity first."
            )
        if report.match_rate < minimum_match_rate:
            raise CommandError(
                f"Refusing to apply: measured match rate {report.match_rate:.4f} is below "
                f"the required {minimum_match_rate:.4f}."
            )

    def apply(
        self,
        report: CoordinateEnrichmentReport,
        candidates: dict[str, tuple[Decimal, Decimal]],
        *,
        minimum_match_rate: float | None,
    ) -> int:
        """Update only coordinates and source provenance after the measured gate passes."""
        self._validate_apply(report, minimum_match_rate)
        accounts = []
        for account in PropertyAccount.objects.filter(tax_year=report.target_year):
            coordinates = candidates.get(account.prop_id)
            if coordinates is None:
                continue
            account.latitude, account.longitude = coordinates
            account.coordinate_source = COORDINATE_SOURCE
            account.coordinate_source_year = report.source_year
            accounts.append(account)
        PropertyAccount.objects.bulk_update(accounts, COORDINATE_FIELDS_UPDATED, batch_size=200)
        return len(accounts)
