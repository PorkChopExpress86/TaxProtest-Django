"""Custom management command: scrape Brazos County's (BCAD) published adopted
tax rates into the shared counties.harris.models.TaxUnitRate table (county="brazos").

Unlike Harris's import_tax_unit_rates (a manual TSV upload), BCAD publishes
adopted rates as a static, scrapable HTML table -- no bulk export file exists
for this (see docs/research/brazos-entity-tax-rates.md on branch
research/brazos-entity-tax-rates, wayfinder ticket #8). This command scrapes
that page directly.

TaxUnitRate is shared with Harris (wayfinder ticket #9): counties.harris.tax_impact
.calculate_tax_impact() is reused verbatim across counties once its three
backing tables carry a matching county's rows. Upserts here are always keyed
with county="brazos" so they never collide with Harris's rows in the same
table, even if a tax_unit_code happens to match (e.g. a short code like "C1"
existing in both counties' data independently).

Usage:
  python manage.py import_brazos_tax_rates [--year YYYY] [--dry-run]
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import requests
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand, CommandError

from counties.harris.models import TaxUnitRate

BCAD_RATES_URL = "https://brazoscad.org/tax-information/adopted-tax-rates/"
USER_AGENT = "TaxProtest-Django/1.0 (+brazos_cad loader)"
SOURCE = "brazoscad_adopted_rates_scrape"

YEAR_RE = re.compile(r"(19|20)\d{2}")
# "G1 – Brazos County" -> code="G1", name="Brazos County"
JURISDICTION_RE = re.compile(r"^(?P<code>\S+)\s*–\s*(?P<name>.+)$")


class Command(BaseCommand):
    help = "Scrape BCAD's published adopted tax rates into the shared TaxUnitRate table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            help="Tax year to record these rates under. If omitted, parsed from the "
            "page's own '<year> TAX RATES' heading.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and print rows without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        requested_year: int | None = options.get("year")

        self.stdout.write(f"Fetching {BCAD_RATES_URL}")
        try:
            response = requests.get(BCAD_RATES_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Failed to fetch BCAD adopted-rates page: {exc}") from exc

        soup = BeautifulSoup(response.text, "lxml")
        tables = soup.find_all("table")
        if not tables:
            raise CommandError(
                f"No <table> found at {BCAD_RATES_URL}. The page layout may have changed."
            )

        rows = tables[0].find_all("tr")
        if len(rows) < 3:
            raise CommandError(
                f"Expected a heading row + header row + data rows at {BCAD_RATES_URL}, "
                f"found only {len(rows)} <tr> elements."
            )

        heading_text = rows[0].get_text(strip=True)
        year_match = YEAR_RE.search(heading_text)
        target_year = requested_year or (int(year_match.group(0)) if year_match else 0)
        if not target_year:
            raise CommandError(
                f"Could not determine a tax year from heading {heading_text!r}. Pass --year YYYY."
            )

        parsed: list[tuple[str, str, str]] = []
        skipped = 0
        for row in rows[2:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            if len(cells) < 4:
                skipped += 1
                continue
            match = JURISDICTION_RE.match(cells[0])
            if not match:
                skipped += 1
                continue
            # BCAD publishes "Total Rate" per Texas convention: dollars per
            # $100 of value (e.g. "$0.419700" for Brazos County). tax_impact
            # .py multiplies taxable_value * adopted_rate directly expecting
            # a true fraction (confirmed against data/tests/test_tax_impact
            # .py's fixtures, e.g. adopted_rate=0.02 for a 2% rate) -- divide
            # by 100 here so $0.419700/$100 becomes the fraction 0.004197,
            # not the raw table value (which would overcharge every Brazos
            # tax estimate by exactly 100x).
            raw_rate = cells[3].lstrip("$").strip()
            try:
                rate = str(Decimal(raw_rate) / 100)
            except InvalidOperation:
                skipped += 1
                continue
            parsed.append((match.group("code").strip(), match.group("name").strip(), rate))

        if skipped:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {skipped} row(s) that didn't match the expected shape."
                )
            )
        if not parsed:
            raise CommandError("No jurisdiction rows parsed; the page layout may have changed.")

        if dry_run:
            for code, name, rate_text in parsed:
                self.stdout.write(f"[dry-run] {target_year} {code} ({name}): {rate_text}")
            return

        upserted = 0
        for code, name, rate_text in parsed:
            TaxUnitRate.objects.update_or_create(
                tax_year=target_year,
                tax_unit_code=code,
                county="brazos",
                defaults={
                    "tax_unit_name": name,
                    "adopted_rate": rate_text,
                    "source": SOURCE,
                },
            )
            upserted += 1

        self.stdout.write(
            self.style.SUCCESS(f"Upserted {upserted} Brazos tax-unit rate rows for {target_year}.")
        )
