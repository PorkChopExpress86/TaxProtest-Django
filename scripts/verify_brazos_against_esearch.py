"""Verify loaded Brazos CAD rows against the district's public property search.

Samples properties from `PropertyAccount`, fetches the same account and tax year
from esearch.brazoscad.org, and compares field by field. This is an end-to-end
check of the fixed-width offsets in data/brazos_layouts.py against the district's
own rendering of the same record.

Value semantics
---------------
Each published figure has a dedicated column, so this compares stored values
directly rather than deriving them:

    esearch "Market Value"    == appraised_val            (sum of the components)
    esearch "Appraised Value" == appraised_val_prod_loss  (after the ag deduction)
    esearch "Assessed Value"  == assessed_val_prod_loss   (after cap and breaker)
    esearch "Circuit Breaker" == circuit_breaker_val

Note that `appraised_val` and `assessed_val` are named misleadingly in the export:
both are computed *before* the agricultural productivity deduction, which is why
the `*_prod_loss` columns exist and are what the district actually publishes.

Personal-property pages render a single "Personal Property Value" and omit the
appraised/land/improvement breakdown, so those fields are skipped for them.

Usage (from the host):
    docker compose exec web python scripts/verify_brazos_against_esearch.py
    docker compose exec web python scripts/verify_brazos_against_esearch.py --limit 6
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from decimal import Decimal

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxprotest.settings")
django.setup()

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from data.models import PropertyAccount  # noqa: E402

BASE = "https://esearch.brazoscad.org/Property/View"
UA = "TaxProtest-Django data verification (contact: repo owner)"
DELAY_SECONDS = 1.5


def page_text(prop_id: int, year: int) -> str:
    response = requests.get(
        f"{BASE}/{prop_id}", params={"year": year}, headers={"User-Agent": UA}, timeout=45
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" "))


def money(text: str, label: str) -> Decimal | None:
    """Pull a $ amount for an exact label. (?<![\\w]) stops 'Land Homesite Value'
    from also matching 'Improvement Homesite Value'."""
    match = re.search(rf"(?<![\w]){re.escape(label)}:?\s*\$\s*([\d,]+)", text)
    return Decimal(match.group(1).replace(",", "")) if match else None


def field(text: str, label: str, stop: str) -> str:
    match = re.search(rf"{re.escape(label)}:?\s*(.*?)\s*(?={re.escape(stop)})", text)
    return match.group(1).strip() if match else ""


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().upper()


def leading_parenthetical(value: str) -> str:
    """Extract the bracketed code from '(CODE) DESCRIPTION'.

    Needs balanced matching, not a regex: neighborhood codes contain parentheses
    of their own, e.g. '(WH(C)) OFFICE/WAREHOUSE(CLASS C)' — where the code is
    'WH(C)'. A greedy or lazy regex takes too much or too little.
    """
    value = value.strip()
    if not value.startswith("("):
        return ""
    depth = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return value[1:index]
    return ""


def scrape(prop_id: int, year: int) -> dict:
    text = page_text(prop_id, year)
    if f"Property ID: {prop_id}" not in text:
        raise ValueError("property id not found on page")
    if f"For Year {year}" not in text:
        raise ValueError(f"page did not honour year={year}")

    hood_code = leading_parenthetical(field(text, "Neighborhood", "Owner"))

    personal = money(text, "Personal Property Value")
    market = money(text, "Market Value")
    appraised = money(text, "Appraised Value")
    # Personal-property pages show only a single value and no breakdown.
    if appraised is None and personal is not None:
        appraised = market

    return {
        "is_personal": personal is not None,
        "circuit_breaker": money(text, "Circuit Breaker") or Decimal(0),
        "situs_address": field(text, "Situs Address", "Map ID"),
        "geo_id": field(text, "Geographic ID", "Type:"),
        "owner_name": field(text, "Name", "Agent:"),
        "legal_desc": field(text, "Legal Description", "Abstract/Subdivision"),
        "abs_subdv_cd": field(text, "Abstract/Subdivision", "Neighborhood"),
        "hood_cd": hood_code,
        "imprv_hstd_val": money(text, "Improvement Homesite Value"),
        "imprv_non_hstd_val": money(text, "Improvement Non-Homesite Value"),
        "land_hstd_val": money(text, "Land Homesite Value"),
        "land_non_hstd_val": money(text, "Land Non-Homesite Value"),
        "ag_market": money(text, "Agricultural Market Valuation"),
        "ag_use_val": money(text, "Ag Use Value"),
        "market_value": market,
        "appraised_value": appraised,
        "assessed_value": money(text, "Assessed Value"),
        "hs_cap_loss": money(text, "HS Cap Loss"),
    }


def expected(row: PropertyAccount) -> dict:
    """What the page should show, read straight from the stored columns."""
    z = Decimal(0)
    return {
        "situs_address": row.situs_address,
        "circuit_breaker": row.circuit_breaker_val if row.circuit_breaker_val is not None else z,
        "geo_id": row.geo_id,
        "owner_name": row.owner_name,
        # esearch renders only the primary line. legal_desc2 is a genuine second
        # field in the export (populated on ~3% of records) that the page does
        # not show, so concatenating them here would report false mismatches.
        "legal_desc": row.legal_desc,
        "abs_subdv_cd": row.abs_subdv_cd,
        "hood_cd": row.hood_cd,
        "imprv_hstd_val": row.imprv_hstd_val or z,
        "imprv_non_hstd_val": row.imprv_non_hstd_val or z,
        "land_hstd_val": row.land_hstd_val or z,
        "land_non_hstd_val": row.land_non_hstd_val or z,
        "ag_market": row.ag_market or z,
        "ag_use_val": row.ag_use_val or z,
        "market_value": row.market_value or z,
        "appraised_value": row.appraised_value or z,
        "assessed_value": row.assessed_value or z,
        "hs_cap_loss": row.ten_percent_cap or z,
    }


# Fields identifying *which* property this is. A disagreement here can never be
# excused as value drift — it means we are comparing the wrong record, or
# reading the wrong bytes.
TEXT_FIELDS = {"geo_id", "owner_name", "legal_desc", "abs_subdv_cd", "hood_cd", "situs_address"}
COMPONENTS = (
    "imprv_hstd_val",
    "imprv_non_hstd_val",
    "land_hstd_val",
    "land_non_hstd_val",
    "ag_market",
)


def self_consistent(values: dict) -> bool:
    """True when market value equals the sum of its own components.

    This is what separates a parsing fault from a data-vintage difference. A bad
    byte offset yields a market value that no longer reconciles with its parts;
    a post-certification supplement changes the parts and the total together, so
    each side stays internally consistent while disagreeing with the other.
    """
    total = sum((values.get(name) or Decimal(0)) for name in COMPONENTS)
    market = values.get("market_value")
    return market is not None and total == market


# Personal-property pages omit the land/improvement breakdown entirely.
PERSONAL_SKIP = {
    "imprv_hstd_val",
    "imprv_non_hstd_val",
    "land_hstd_val",
    "land_non_hstd_val",
    "ag_market",
    "ag_use_val",
    "hs_cap_loss",
}


def compare(exp: dict, got: dict) -> list[str]:
    """Return a list of mismatch descriptions; empty means every field agreed."""
    problems = []
    for key, want in exp.items():
        if got.get("is_personal") and key in PERSONAL_SKIP:
            continue

        if key == "situs_address":
            # The export carries number/prefix/street/suffix; the page appends
            # city, state and ZIP, which are blank on 99.8% of export rows. A
            # prefix check is the strongest comparison the data supports.
            if not want:
                continue
            if not norm(str(got.get(key) or "")).startswith(norm(str(want))):
                problems.append(f"situs_address: export={want!r} page={got.get(key)!r}")
            continue
        have = got.get(key)
        if have is None:
            # Not every page renders every row (ag/timber blocks are omitted when
            # zero). Absent-but-expected-zero is agreement, not a mismatch.
            if key not in TEXT_FIELDS and want == 0:
                continue
            problems.append(f"{key}: page missing (export={want})")
            continue
        if key in TEXT_FIELDS:
            if norm(str(want)) != norm(str(have)):
                problems.append(f"{key}: export={want!r} page={have!r}")
        elif Decimal(want) != Decimal(have):
            problems.append(f"{key}: export={want:,} page={have:,}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--limit", type=int, default=10, help="properties per city")
    parser.add_argument(
        "--seed",
        type=int,
        help="Sample randomly across the whole city pool with this seed. Without it "
        "the lowest prop_ids are taken, which tends to cluster in one subdivision.",
    )
    args = parser.parse_args()

    sample: list[PropertyAccount] = []
    for city in ("BRYAN", "COLLEGE STATION"):
        pool = list(
            PropertyAccount.objects.filter(tax_year=args.year, situs_city=city).order_by("prop_id")
        )
        if args.seed is not None:
            rng = random.Random(f"{args.seed}-{city}")
            sample += rng.sample(pool, min(args.limit, len(pool)))
        else:
            sample += pool[: args.limit]

    print(f"Verifying {len(sample)} properties against esearch.brazoscad.org ({args.year})\n")

    passed, failed, errors, drifted = 0, 0, 0, 0
    notes: list[str] = []
    for index, row in enumerate(sample, 1):
        label = f"[{index:2}/{len(sample)}] {row.prop_id} {row.situs_city:<15}"
        try:
            got = scrape(row.prop_id, args.year)
        except Exception as exc:  # noqa: BLE001 — one bad fetch shouldn't end the run
            print(f"{label} ERROR  {exc}")
            errors += 1
            continue

        tags = []
        if got["is_personal"]:
            tags.append("personal property")
        if row.legal_desc2:
            tags.append(f"legal_desc2={row.legal_desc2!r} (export only)")
        if got["circuit_breaker"]:
            tags.append(f"circuit breaker -{got['circuit_breaker']:,}")
        suffix = f"  [{'; '.join(tags)}]" if tags else ""

        exp = expected(row)
        problems = compare(exp, got)
        if problems:
            identity_ok = not any(p.split(":")[0] in TEXT_FIELDS for p in problems)
            both_consistent = got["is_personal"] or (self_consistent(exp) and self_consistent(got))
            if identity_ok and both_consistent:
                # Same property, each side internally coherent: the certified
                # roll and the live site simply disagree on value.
                drifted += 1
                delta = (got["market_value"] or Decimal(0)) - exp["market_value"]
                print(
                    f"{label} DRIFT  certified {exp['market_value']:,} -> site {got['market_value']:,}{suffix}"
                )
                notes.append(
                    f"{row.prop_id}: value moved {delta:+,} since certification "
                    f"(sup_num={row.sup_num}; esearch shows post-certification supplements)"
                )
                time.sleep(DELAY_SECONDS)
                continue
            failed += 1
            print(f"{label} MISMATCH ({len(problems)}){suffix}")
            for problem in problems:
                print(f"          - {problem}")
        else:
            passed += 1
            market = got["market_value"] or Decimal(0)
            print(f"{label} OK     {market:>12,} market  {row.owner_name[:26]}{suffix}")

        time.sleep(DELAY_SECONDS)

    total = passed + failed + errors + drifted
    print(f"\n{'=' * 66}")
    print(
        f"matched {passed}/{total}   value drift {drifted}   "
        f"mismatched {failed}   fetch errors {errors}"
    )
    if notes:
        print("\nExplained differences (not ETL faults):")
        for note in notes:
            print(f"  - {note}")
    # Drift is expected against a live site and does not fail the run; a real
    # field mismatch does.
    return 1 if (failed or errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
