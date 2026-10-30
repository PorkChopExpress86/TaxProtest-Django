from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models import Count

from counties.common.tax_models import AssessmentHistory
from counties.harris.etl_pipeline.row_reader import iter_dict_rows
from counties.harris.replacement_guard import check_replacement
from counties.harris.source_catalog import (
    DEFAULT_HCAD_SOURCE_CATALOG,
    REAL_ACCT_MEMBER_PATH,
    HcadSourceId,
)

REAL_ACCT_REQUIRED = ("acct", "assessed_val")
HEARING_REQUIRED = ("acct", "Final_Appraised_Value")


# The hearing member path relative to a year root.
HEARING_MEMBER = DEFAULT_HCAD_SOURCE_CATALOG.member_path(
    HcadSourceId.HEARING_FILES, "arb_hearings_real.txt"
)


def _parse_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip().replace(",", "").replace("$", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


@dataclass
class HistoryImportCounts:
    years_processed: int = 0
    records_loaded: int = 0
    tax_year_counts: dict[int, int] = field(default_factory=dict)
    source_years: set[int] = field(default_factory=set)


class AssessmentHistoryImporter:
    """Import year-aware assessed value history from HCAD snapshots."""

    def __init__(self, *, batch_size: int = 5000):
        self.batch_size = batch_size
        self.counts = HistoryImportCounts()

    def import_year_range(
        self, start_year: int, end_year: int, extract_root: Path
    ) -> HistoryImportCounts:
        counts = self.counts = HistoryImportCounts()
        years = list(range(start_year, end_year + 1))

        with transaction.atomic():
            # county="harris": this importer reads HCAD snapshots only. AssessmentHistory
            # is now a shared table (see wayfinder ticket #9) — an unscoped delete here
            # would also wipe Brazos's rows for the same tax-year range.
            existing = AssessmentHistory.objects.filter(
                tax_year__gte=start_year, tax_year__lte=end_year, county="harris"
            )
            replacing = dict(existing.order_by().values_list("tax_year").annotate(rows=Count("id")))
            existing.delete()

            for year in years:
                year_root = extract_root / str(year)
                loaded = self.import_year(year, year_root)
                # Raising here rolls the delete back along with the partial load.
                check_replacement(
                    f"Harris AssessmentHistory {year}",
                    replacing=replacing.get(year, 0),
                    staged=loaded,
                )
                counts.records_loaded += loaded
                counts.years_processed += 1

        return counts

    def import_year(self, year: int, year_root: Path) -> int:
        real_acct_path = year_root / REAL_ACCT_MEMBER_PATH
        hearing_path = year_root / HEARING_MEMBER

        if not real_acct_path.exists():
            raise FileNotFoundError(f"Missing real account file for {year}: {real_acct_path}")

        hearing_rows = self._load_hearing_rows(hearing_path, year) if hearing_path.exists() else {}
        loaded = 0
        batch: list[AssessmentHistory] = []

        for row in iter_dict_rows(real_acct_path, REAL_ACCT_REQUIRED):
            account_number = str(row.get("acct", "")).strip()
            if not account_number:
                continue

            hearing = hearing_rows.pop(account_number, None)
            history = self._build_history_from_real_acct(account_number, row, year, hearing)
            observed_year = _parse_int(row.get("yr"))
            if observed_year is not None:
                self.counts.source_years.add(observed_year)
            self.counts.tax_year_counts[history.tax_year] = (
                self.counts.tax_year_counts.get(history.tax_year, 0) + 1
            )
            batch.append(history)

            if len(batch) >= self.batch_size:
                AssessmentHistory.objects.bulk_create(batch, batch_size=self.batch_size)
                loaded += len(batch)
                batch.clear()

        for account_number, hearing in hearing_rows.items():
            history = self._build_history_from_hearing_only(account_number, year, hearing)
            self.counts.tax_year_counts[history.tax_year] = (
                self.counts.tax_year_counts.get(history.tax_year, 0) + 1
            )
            batch.append(history)
            if len(batch) >= self.batch_size:
                AssessmentHistory.objects.bulk_create(batch, batch_size=self.batch_size)
                loaded += len(batch)
                batch.clear()

        if batch:
            AssessmentHistory.objects.bulk_create(batch, batch_size=self.batch_size)
            loaded += len(batch)

        return loaded

    def _load_hearing_rows(
        self, hearing_path: Path, default_year: int
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for row in iter_dict_rows(hearing_path, HEARING_REQUIRED):
            account_number = str(row.get("acct", "")).strip()
            if not account_number:
                continue
            tax_year = _parse_int(row.get("Tax_Year")) or default_year
            observed_year = _parse_int(row.get("Tax_Year"))
            if observed_year is not None:
                self.counts.source_years.add(observed_year)
            rows[account_number] = {
                "tax_year": tax_year,
                "final_appraised_value": _parse_decimal(row.get("Final_Appraised_Value")),
            }
        return rows

    def _build_history_from_real_acct(
        self,
        account_number: str,
        row: dict[str, Any],
        default_year: int,
        hearing: dict[str, Any] | None,
    ) -> AssessmentHistory:
        certified_value = _parse_decimal(row.get("assessed_val"))
        final_appraised_value = hearing.get("final_appraised_value") if hearing else None
        tax_year = (
            _parse_int(row.get("yr"))
            or (hearing.get("tax_year") if hearing else None)
            or default_year
        )
        assessed_value = (
            final_appraised_value if final_appraised_value is not None else certified_value
        )

        return AssessmentHistory(
            account_number=account_number,
            tax_year=tax_year,
            assessed_value=assessed_value,
            appraised_value=_parse_decimal(row.get("tot_appr_val")),
            market_value=_parse_decimal(row.get("tot_mkt_val")),
            prior_appraised_value=_parse_decimal(row.get("prior_tot_appr_val")),
            prior_market_value=_parse_decimal(row.get("prior_tot_mkt_val")),
            new_construction_value=_parse_decimal(row.get("new_construction_val")),
            cap_account=str(row.get("Cap_acct", "") or "").strip(),
        )

    def _build_history_from_hearing_only(
        self,
        account_number: str,
        default_year: int,
        hearing: dict[str, Any],
    ) -> AssessmentHistory:
        final_appraised_value = hearing.get("final_appraised_value")

        return AssessmentHistory(
            account_number=account_number,
            tax_year=hearing.get("tax_year") or default_year,
            assessed_value=final_appraised_value,
            appraised_value=final_appraised_value,
        )
