"""Load multi-year assessed-value history for Brazos County from BCAD's
certified-data-downloads archive (issue #13).

BCAD keeps roughly a decade of past certified exports available at
``https://brazoscad.org/certified-data-downloads/`` (2016-2025 confirmed live
as of 2026-08), each a standalone ``bcad_certified_<year>.zip`` in the same
17-file PACS format ``load_brazos_cad`` already parses for the current year.
Unlike HCAD's ``real_acct.txt``, no file in this export carries a prior-year
value column -- there is no "diff two years" step here. Each year's own
``APPRAISAL_ENTITY_INFO.TXT`` already has that year's assessed/appraised/
market value, so this command is a per-year download-extract-parse loop, one
``AssessmentHistory`` row written directly per (property, year). The
year-to-year comparison ``evaluate_cap_status`` needs comes for free once
multiple years' rows exist in the table: it walks consecutive tax_year rows
itself (see ``counties.common.history.assessment_history_rows``), the same
way it already does for Harris.

Field mapping, all from ``APPRAISAL_ENTITY_INFO.TXT`` (``parsers/pacs.py``'s
``ENTITY_INFO_LAYOUT``) -- verified against two real exports of different
record lengths (2022: 2,614 bytes/line; 2025: 2,750 bytes/line, cross-checked
against the live-verified ``PropertyAccount.assessed_value``; see that
layout's docstring for the full evidence):

  AssessmentHistory.assessed_value  <- assessed_value   (same field already
                                        used for PropertyAccount's rollup)
  AssessmentHistory.appraised_value <- appraised_value
  AssessmentHistory.market_value    <- market_value
  AssessmentHistory.cap_account     <- "Y" if appraised_value > assessed_value
                                        else "" (a value-capping reduction
                                        was applied that year; see the
                                        layout's docstring for why this
                                        reflects the cap specifically and not
                                        an entity-specific exemption)

Deliberately NOT populated: ``prior_appraised_value``/``prior_market_value``
(HCAD carries these directly in ``real_acct.txt``; nothing in BCAD's export
does, but nothing needs them either -- ``evaluate_cap_status`` falls back to
an actual prior-year row when one exists, which multi-year loading supplies)
and ``new_construction_value`` (no verified BCAD source; the cap calculation
already defaults a missing value to $0, same as Harris when it lacks one).

APPRAISAL_ENTITY_INFO.TXT is a property-times-taxing-entity file (roughly
five to ten rows per property); ~99% of rows agree on
market_value/appraised_value/assessed_value across every entity for the same
property, so this command rolls up to one row per property by keeping the
first entity row seen per prop_id, mirroring ``load_brazos_cad``'s existing
``assessed_value_by_prop`` rollup for ``PropertyAccount``.

One more real-world wrinkle, confirmed while loading 2021-2025: BCAD's 2023
certified export is DEFLATE64-compressed, which Python's zipfile has never
supported (``NotImplementedError``, a long-standing stdlib gap -- not
specific to this archive or this project's handling of it). ``_extract``
falls back to the system ``7z`` binary (``p7zip-full``, added to the
Dockerfile) for exactly the archives that trip this.

Usage:
    python manage.py import_brazos_assessment_history
    python manage.py import_brazos_assessment_history --start-year 2021 --end-year 2025
    python manage.py import_brazos_assessment_history --years 3 --all-accounts
"""

from __future__ import annotations

import logging
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from counties.brazos.models import PropertyAccount
from counties.brazos.parsers.pacs import parse_entity_info_line
from counties.harris.models import AssessmentHistory

logger = logging.getLogger("brazos_cad")

PORTAL_URL = "https://brazoscad.org/certified-data-downloads/"
USER_AGENT = "TaxProtest-Django/1.0 (+brazos_cad loader)"
DOWNLOAD_TIMEOUT = 300  # seconds

YEAR_RE = re.compile(r"(19|20)\d{2}")
ZIP_RE = re.compile(r"\.zip$", re.IGNORECASE)

ENTITY_INFO_FILENAME = "APPRAISAL_ENTITY_INFO.TXT"

#: How many recent years to default to when --start-year/--end-year are
#: omitted, matching Harris's import_assessment_history default window.
DEFAULT_YEAR_SPAN = 5

#: Below this fraction of rolled-up properties satisfying
#: market_value >= appraised_value >= assessed_value, refuse to load a year.
#: Guards against an unverified year (this project has only checked 2022 and
#: 2025) turning out to have a different field layout -- see the module and
#: ENTITY_INFO_LAYOUT docstrings for how this was verified for those two.
MIN_SANE_ORDER_FRACTION = Decimal("0.90")

COUNTY = "brazos"


@dataclass(frozen=True)
class YearRollup:
    account_number: str
    assessed_value: Decimal | None
    appraised_value: Decimal | None
    market_value: Decimal | None

    @property
    def cap_account(self) -> str:
        if self.appraised_value is not None and self.assessed_value is not None:
            if self.appraised_value > self.assessed_value:
                return "Y"
        return ""

    @property
    def is_sanely_ordered(self) -> bool:
        """market >= appraised >= assessed, or vacuously true if any is missing."""
        market, appraised, assessed = self.market_value, self.appraised_value, self.assessed_value
        if market is None or appraised is None or assessed is None:
            return True
        return market >= appraised >= assessed


class Command(BaseCommand):
    help = "Import multi-year Brazos assessed/appraised/market value history from BCAD."

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int, help="First tax year to load (inclusive).")
        parser.add_argument(
            "--end-year",
            type=int,
            help="Last tax year to load (inclusive). Default: the newest year BCAD's "
            "portal offers.",
        )
        parser.add_argument(
            "--years",
            type=int,
            default=DEFAULT_YEAR_SPAN,
            help=f"When --start-year is omitted, how many years back from --end-year "
            f"to load (default {DEFAULT_YEAR_SPAN}).",
        )
        parser.add_argument(
            "--force", action="store_true", help="Re-download even if already on disk."
        )
        parser.add_argument(
            "--skip-download",
            action="store_true",
            help="Use archives already on disk; do not scrape or download.",
        )
        parser.add_argument(
            "--skip-extract",
            action="store_true",
            help="Use files already extracted; do not re-extract archives.",
        )
        parser.add_argument(
            "--all-accounts",
            action="store_true",
            help="Load every account in each year's export, not just accounts already "
            "ingested as PropertyAccount rows (the export covers ~1.6M accounts "
            "including commercial and personal property).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Download/extract/parse/verify without writing to the database.",
        )

    # ------------------------------------------------------------------ portal

    def _list_archives(self, portal_url: str) -> dict[int, str]:
        """Every (year -> absolute .zip url) pair BCAD's portal currently offers.

        Unlike load_brazos_cad's _scrape_archive (which keeps only the
        highest year), every link on this page is a certified-year archive --
        no monthly/map-book noise to filter, unlike the GIS portal.
        """
        self.stdout.write(f"Scraping BCAD portal: {portal_url}")
        try:
            response = requests.get(portal_url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Failed to fetch BCAD portal: {exc}") from exc

        soup = BeautifulSoup(response.text, "lxml")
        archives: dict[int, str] = {}
        for anchor in soup.find_all("a", href=True):
            # str(): bs4 types an attribute value as possibly a list (the
            # multi-valued-attribute case, e.g. `class`), which href never is.
            href = str(anchor["href"])
            text = anchor.get_text(" ", strip=True)
            if not ZIP_RE.search(href):
                continue
            match = YEAR_RE.search(text) or YEAR_RE.search(href)
            if not match:
                continue
            year = int(match.group(0))
            archives[year] = requests.compat.urljoin(portal_url, href)

        if not archives:
            raise CommandError(
                f"No dated .zip archive links found at {portal_url}. "
                "The portal layout may have changed."
            )
        self.stdout.write(
            f"  found {len(archives)} certified years: {sorted(archives)[0]}-{sorted(archives)[-1]}"
        )
        return archives

    # ------------------------------------------------------------------ download/extract

    def _download(self, url: str, destination: Path, *, force: bool, dry_run: bool) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not force:
            self.stdout.write(f"  archive already on disk: {destination}")
            return destination
        if dry_run:
            self.stdout.write(f"  [dry-run] would download {url} -> {destination}")
            return destination

        self.stdout.write(f"  downloading {url} -> {destination}")
        try:
            with requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=DOWNLOAD_TIMEOUT, stream=True
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        fh.write(chunk)
        except requests.RequestException as exc:
            raise CommandError(f"Failed to download {url}: {exc}") from exc
        return destination

    def _extract(self, archive: Path, extract_dir: Path, *, dry_run: bool) -> None:
        if not archive.exists():
            raise CommandError(f"Archive not found: {archive}")
        if dry_run:
            self.stdout.write(f"  [dry-run] would extract {archive} -> {extract_dir}")
            return
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile as exc:
            raise CommandError(f"Archive is not a valid zip: {archive} ({exc})") from exc
        except NotImplementedError:
            # Confirmed real: BCAD's 2023 certified export uses DEFLATE64
            # compression, which Python's zipfile has never supported (a
            # long-standing stdlib gap, not a bug in this archive). `7z`
            # (p7zip-full, see Dockerfile) does support it.
            self.stdout.write(
                "  zipfile can't decompress this archive (likely DEFLATE64) -- "
                "falling back to 7z"
            )
            self._extract_with_7z(archive, extract_dir)

    @staticmethod
    def _extract_with_7z(archive: Path, extract_dir: Path) -> None:
        import shutil
        import subprocess

        if shutil.which("7z") is None:
            raise CommandError(
                f"{archive} needs `7z` to extract (zipfile doesn't support its "
                "compression method) but `7z` isn't installed. Install p7zip-full."
            )
        result = subprocess.run(
            ["7z", "x", f"-o{extract_dir}", "-y", str(archive)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise CommandError(
                f"7z failed to extract {archive} (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    @staticmethod
    def _resolve_entity_info_file(extract_dir: Path) -> Path | None:
        """Real BCAD filenames carry a timestamp prefix and may sit in a
        nested directory (confirmed: 2025 is flat, 2022 nests everything
        under "2022 CERTIFICATION EXPORT/") -- search recursively, take the
        lexicographically-last match if extraction left more than one."""
        matches = sorted(extract_dir.rglob(f"*{ENTITY_INFO_FILENAME}"), reverse=True)
        return matches[0] if matches else None

    # ------------------------------------------------------------------ parse

    @staticmethod
    def _iter_lines(path: Path) -> Iterator[str]:
        with path.open("r", encoding="latin-1", newline="") as fh:
            for line in fh:
                line = line.rstrip("\r\n")
                if line.strip():
                    yield line

    def _roll_up_year(
        self, entity_info_path: Path, tax_year: int, accounts: set[str] | None
    ) -> list[YearRollup]:
        seen: dict[str, YearRollup] = {}
        mismatched = 0
        skipped_unscoped = 0

        for line in self._iter_lines(entity_info_path):
            fields = parse_entity_info_line(line)
            record_year = fields.get("tax_year")
            if record_year is not None and record_year != tax_year:
                mismatched += 1
                continue

            prop_id = fields["prop_id"]
            if not prop_id or prop_id in seen:
                continue
            if accounts is not None and prop_id not in accounts:
                skipped_unscoped += 1
                continue

            seen[prop_id] = YearRollup(
                account_number=prop_id,
                assessed_value=fields.get("assessed_value"),
                appraised_value=fields.get("appraised_value"),
                market_value=fields.get("market_value"),
            )

        if mismatched:
            logger.warning(
                "%s (%s): skipped %d rows whose in-record tax_year didn't match requested %d",
                ENTITY_INFO_FILENAME,
                tax_year,
                mismatched,
                tax_year,
            )
        if accounts is not None:
            self.stdout.write(
                f"  scoped to already-ingested accounts: kept {len(seen):,}, "
                f"skipped {skipped_unscoped:,} not in PropertyAccount"
            )
        return list(seen.values())

    def _verify_sane_order(self, tax_year: int, rollups: list[YearRollup]) -> None:
        checkable = [r for r in rollups if r.market_value is not None]
        if not checkable:
            raise CommandError(
                f"No rows for tax_year {tax_year} had a market_value -- refusing to "
                "trust this year's field layout. Confirm APPRAISAL_ENTITY_INFO.TXT "
                "for this year matches the offsets documented in parsers/pacs.py's "
                "ENTITY_INFO_LAYOUT before retrying."
            )
        sane = sum(1 for r in checkable if r.is_sanely_ordered)
        fraction = Decimal(sane) / Decimal(len(checkable))
        self.stdout.write(
            f"  sanity check: {sane:,}/{len(checkable):,} properties satisfy "
            f"market >= appraised >= assessed ({fraction:.1%})"
        )
        if fraction < MIN_SANE_ORDER_FRACTION:
            raise CommandError(
                f"Only {fraction:.1%} of tax_year {tax_year} rows satisfy "
                f"market >= appraised >= assessed (need >= {MIN_SANE_ORDER_FRACTION:.0%}). "
                "This year's export likely uses a different record layout than the "
                "ones verified in ENTITY_INFO_LAYOUT's docstring -- refusing to load "
                "it rather than write unreliable values. Re-verify the byte offsets "
                "against this year's real export before retrying."
            )

    # ------------------------------------------------------------------ main

    def handle(self, *args, **options):
        force: bool = options["force"]
        skip_download: bool = options["skip_download"]
        skip_extract: bool = options["skip_extract"]
        dry_run: bool = options["dry_run"]
        all_accounts: bool = options["all_accounts"]

        download_dir = Path(settings.BCAD_DOWNLOAD_DIR)
        extract_root = Path(settings.BCAD_EXTRACT_DIR)

        archives: dict[int, str] = {}
        if not skip_download:
            archives = self._list_archives(PORTAL_URL)

        end_year = options.get("end_year")
        if end_year is None:
            if archives:
                end_year = max(archives)
            else:
                raise CommandError(
                    "--end-year is required when --skip-download is set (no portal "
                    "scrape to infer it from)."
                )
        start_year = options.get("start_year")
        if start_year is None:
            start_year = end_year - options["years"] + 1
        if start_year > end_year:
            raise CommandError("--start-year must be less than or equal to --end-year")

        years = list(range(start_year, end_year + 1))
        self.stdout.write(f"Loading Brazos assessment history for {years}")

        accounts: set[str] | None = None
        if not all_accounts:
            accounts = set(PropertyAccount.objects.values_list("prop_id", flat=True))
            if not accounts:
                raise CommandError(
                    "No PropertyAccount rows exist to scope the import to. Run "
                    "load_brazos_cad first, or pass --all-accounts."
                )

        per_year_rollups: dict[int, list[YearRollup]] = {}

        for year in years:
            self.stdout.write(f"\n== {year} ==")
            archive = download_dir / f"bcad_certified_{year}.zip"
            extract_dir = extract_root / str(year)

            if not skip_download:
                url = archives.get(year)
                if url is None:
                    raise CommandError(
                        f"No certified archive for {year} found on BCAD's portal. "
                        f"Available years: {sorted(archives)}"
                    )
                self._download(url, archive, force=force, dry_run=dry_run)
            elif not archive.exists():
                raise CommandError(
                    f"--skip-download set but archive not found: {archive}. Drop "
                    "--skip-download, or pass --year-scoped archives already on disk."
                )

            if not skip_extract:
                self._extract(archive, extract_dir, dry_run=dry_run)

            if dry_run:
                self.stdout.write(f"  [dry-run] would parse and load {year}")
                continue

            entity_info_path = self._resolve_entity_info_file(extract_dir)
            if entity_info_path is None:
                raise CommandError(
                    f"No {ENTITY_INFO_FILENAME} found under {extract_dir} for {year}. "
                    "Did extraction succeed?"
                )

            rollups = self._roll_up_year(entity_info_path, year, accounts)
            self.stdout.write(f"  rolled up {len(rollups):,} properties for {year}")
            self._verify_sane_order(year, rollups)
            per_year_rollups[year] = rollups

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[dry-run] nothing written"))
            return

        total_written = 0
        with transaction.atomic():
            # county="brazos": AssessmentHistory is shared with Harris (wayfinder
            # ticket #9) -- an unscoped delete would also wipe Harris's rows for
            # the same tax-year range.
            AssessmentHistory.objects.filter(
                tax_year__gte=start_year, tax_year__lte=end_year, county=COUNTY
            ).delete()

            for year, rollups in per_year_rollups.items():
                instances = [
                    AssessmentHistory(
                        account_number=r.account_number,
                        tax_year=year,
                        county=COUNTY,
                        assessed_value=r.assessed_value,
                        appraised_value=r.appraised_value,
                        market_value=r.market_value,
                        cap_account=r.cap_account,
                    )
                    for r in rollups
                ]
                AssessmentHistory.objects.bulk_create(instances, batch_size=5000)
                total_written += len(instances)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nLoaded {total_written:,} AssessmentHistory rows for "
                f"{len(per_year_rollups)} year(s) ({start_year}-{end_year})."
            )
        )
