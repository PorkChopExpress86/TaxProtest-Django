from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.db import transaction

from data.models import AssessmentHistory

TEN_PERCENT_CAP = Decimal("10")
TWENTY_PERCENT_CAP = Decimal("20")
ONE_HUNDRED = Decimal("100")
CENT = Decimal("0.01")


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


def _money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _percent_change(current: Decimal | None, prior: Decimal | None) -> Decimal | None:
    if current is None or prior is None or prior <= 0:
        return None
    return ((current - prior) / prior * ONE_HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def _has_cap_account(entry: AssessmentHistory) -> bool:
    return bool(str(entry.cap_account or "").strip())


def evaluate_cap_status(
    current: AssessmentHistory,
    prior: AssessmentHistory | None = None,
) -> dict[str, Any]:
    """Evaluate assessed/appraised value increase against Texas cap thresholds."""
    prior_value = current.prior_appraised_value or (prior.appraised_value if prior else None)
    if prior_value is None and prior:
        prior_value = prior.assessed_value

    current_value = current.appraised_value or current.assessed_value
    market_value = current.market_value
    new_construction = current.new_construction_value or Decimal("0")
    increase_percent = _percent_change(current_value, prior_value)

    cap_type = "homestead" if _has_cap_account(current) else "circuit_breaker"
    limit_percent = TEN_PERCENT_CAP if cap_type == "homestead" else TWENTY_PERCENT_CAP

    if current_value is None or prior_value is None:
        return {
            "status": "unknown",
            "label": "Needs review",
            "cap_type": cap_type,
            "limit_percent": limit_percent,
            "increase_percent": increase_percent,
            "allowed_value": None,
            "overage": None,
        }

    allowed_by_cap = prior_value * (Decimal("1") + (limit_percent / ONE_HUNDRED))
    allowed_by_cap += new_construction
    allowed_value = (
        min(allowed_by_cap, market_value) if market_value is not None else allowed_by_cap
    )
    allowed_value = _money(allowed_value)
    overage = _money(current_value - allowed_value) if allowed_value is not None else None
    status = "over_limit" if overage is not None and overage > 0 else "within_limit"

    return {
        "status": status,
        "label": "Over cap" if status == "over_limit" else "Within cap",
        "cap_type": cap_type,
        "limit_percent": limit_percent,
        "increase_percent": increase_percent,
        "allowed_value": allowed_value,
        "overage": overage,
    }


@dataclass
class HistoryImportCounts:
    years_processed: int = 0
    records_loaded: int = 0


class AssessmentHistoryImporter:
    """Import year-aware assessed value history from HCAD snapshots."""

    def __init__(self, *, batch_size: int = 5000):
        self.batch_size = batch_size

    def import_year_range(
        self, start_year: int, end_year: int, extract_root: Path
    ) -> HistoryImportCounts:
        counts = HistoryImportCounts()
        years = list(range(start_year, end_year + 1))

        with transaction.atomic():
            AssessmentHistory.objects.filter(
                tax_year__gte=start_year, tax_year__lte=end_year
            ).delete()

            for year in years:
                year_root = extract_root / str(year)
                counts.records_loaded += self.import_year(year, year_root)
                counts.years_processed += 1

        return counts

    def import_year(self, year: int, year_root: Path) -> int:
        real_acct_path = year_root / "Real_acct_owner" / "real_acct.txt"
        hearing_path = year_root / "Hearing_files" / "arb_hearings_real.txt"

        if not real_acct_path.exists():
            raise FileNotFoundError(f"Missing real account file for {year}: {real_acct_path}")

        hearing_rows = self._load_hearing_rows(hearing_path, year) if hearing_path.exists() else {}
        loaded = 0
        batch: list[AssessmentHistory] = []

        with real_acct_path.open(encoding="utf-8", errors="ignore", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                account_number = str(row.get("acct", "")).strip()
                if not account_number:
                    continue

                hearing = hearing_rows.pop(account_number, None)
                history = self._build_history_from_real_acct(account_number, row, year, hearing)
                batch.append(history)

                if len(batch) >= self.batch_size:
                    AssessmentHistory.objects.bulk_create(batch, batch_size=self.batch_size)
                    loaded += len(batch)
                    batch.clear()

        for account_number, hearing in hearing_rows.items():
            batch.append(self._build_history_from_hearing_only(account_number, year, hearing))
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
        with hearing_path.open(encoding="utf-8", errors="ignore", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                account_number = str(row.get("acct", "")).strip()
                if not account_number:
                    continue
                tax_year = _parse_int(row.get("Tax_Year")) or default_year
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
