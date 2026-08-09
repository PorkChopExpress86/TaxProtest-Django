"""Custom management command: download and load BCAD's GIS parcel shapefile
into brazos_cad.models.PropertyAccount.

Unlike Harris's load_gis_data (lat/long only), this shapefile is the primary
source for several PropertyAccount fields that don't exist anywhere in the
fixed-width certified export: situs (physical) address, coordinates,
state_class, living_area, year_built, and class_code. See
docs/research/brazos-gis-parcel-shapefile.md on branch
research/brazos-gis-parcel-shapefile (wayfinder tickets #4-#7) for the full
field survey this command implements.

NOT sourced from here: total_value/land_value/improvement_value/
assessed_value. The shapefile's "<year> Certified Shapefiles Download" link
turns out to be a rolling current/preliminary snapshot, not a frozen
certified-year archive -- confirmed against BCAD's own live property search
(esearch.brazoscad.org) for a real property, where the shapefile's value
matched the *next* tax year's page, not the target year's, even though
living_area/year_built/class_code/situs happened to match (those are
structurally stable fields, which is why the mismatch was easy to miss until
checked against dollar values specifically). assessed_value is instead
rolled up in load_brazos_cad.py from APPRAISAL_ENTITY_INFO.TXT, which is
genuinely tax_year-accurate; total_value/land_value/improvement_value have
no verified tax_year-accurate Brazos source yet and are left unpopulated
rather than silently wrong.

This command UPDATES existing PropertyAccount rows (via bulk_update, joined
on prop_id + tax_year) rather than the delete-then-recreate pattern
load_brazos_cad uses for the fixed-width files -- PropertyAccount rows must
already exist (from load_brazos_cad) before this command has anything to
join against. Always run this AFTER load_brazos_cad, and re-run it again
any time load_brazos_cad is re-run for the same year -- load_brazos_cad's
APPRAISAL_INFO.TXT step fully deletes and recreates PropertyAccount rows,
which silently wipes whatever this command wrote (see load_brazos_cad's
docstring for the ordering note).

Pipeline:
  1. Scrape https://brazoscad.org/tax-information/gis/ for the latest
     "... Certified Shapefiles Download" .zip link (the page also lists
     older years, a monthly-update variant, and unrelated map-book
     downloads, so link text -- not just file extension -- must be checked).
  2. Stream-download the archive into BCAD_DOWNLOAD_DIR (idempotent unless
     --force).
  3. Extract to BCAD_EXTRACT_DIR/gis/ and locate the .shp file.
  4. Read via geopandas, reproject EPSG:2277 (NAD83 / Texas Central, US
     feet) -> EPSG:4326 (WGS84) for lat/long, same as Harris's
     data/etl.py::load_gis_parcels.
  5. Zero-pad the shapefile's plain-integer PROP_ID to match
     PropertyAccount.prop_id's 12-char zero-padded string convention, and
     bulk_update matching PropertyAccount rows for --year.

Usage:
  python manage.py load_brazos_gis [--year YYYY] [--force]
      [--skip-download] [--skip-extract] [--dry-run]
"""

from __future__ import annotations

import logging
import math
import re
import zipfile
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from brazos_cad.models import PropertyAccount

logger = logging.getLogger("brazos_cad")

GIS_PORTAL_URL = "https://brazoscad.org/tax-information/gis/"
USER_AGENT = "TaxProtest-Django/1.0 (+brazos_cad loader)"
DOWNLOAD_TIMEOUT = 300  # seconds

YEAR_RE = re.compile(r"(19|20)\d{2}")
ZIP_RE = re.compile(r"\.zip$", re.IGNORECASE)
# Matches "2025 Certified Shapefiles Download" but not "Brazos County Monthly
# Shapefiles Download" or "Brazos County Map Book Download" -- both real
# links on the same page that aren't parcel-boundary data for a specific year.
CERTIFIED_LINK_RE = re.compile(r"certified\s+shapefiles", re.IGNORECASE)

PROPERTY_FIELDS_UPDATED = (
    "situs_address",
    "situs_state",
    "state_class",
    "latitude",
    "longitude",
    "living_area",
    "year_built",
    "class_code",
)


def _is_nan(value: object) -> bool:
    try:
        return math.isnan(value)  # type: ignore[arg-type]
    except TypeError:
        return False


def _clean_str(value: object) -> str:
    """Blank/NaN -> "". Also strips the literal text "NULL" -- confirmed the
    real export's convention for a missing string field (e.g. situs_stre is
    the literal string 'NULL' on 71,239/77,433 real rows, not an actual
    null/NaN), NOT a placeholder to display or concatenate into an address."""
    if value is None or _is_nan(value):
        return ""
    text = str(value).strip()
    return "" if text.upper() == "NULL" else text


def _join_address(*parts: object) -> str:
    return " ".join(p for p in (_clean_str(part) for part in parts) if p)


def _clean_decimal(value: object):
    if value is None or _is_nan(value):
        return None
    return value


def _clean_int(value: object) -> int | None:
    if value is None or _is_nan(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = "Download and load BCAD's GIS parcel shapefile into PropertyAccount."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            help="PropertyAccount.tax_year to update. If omitted, the latest tax_year "
            "already present in PropertyAccount is used.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-download even if the archive already exists on disk.",
        )
        parser.add_argument(
            "--skip-download",
            action="store_true",
            help="Skip the scrape + download step (use the archive already on disk).",
        )
        parser.add_argument(
            "--skip-extract",
            action="store_true",
            help="Skip the extraction step (use an already-extracted shapefile).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log every action without writing to disk or the database.",
        )

    # ------------------------------------------------------------------ scrape

    def _scrape_archive(self, portal_url: str) -> tuple[str, int]:
        """Return (absolute_url, year) for the latest certified shapefile archive."""
        self.stdout.write(f"Scraping BCAD GIS portal: {portal_url}")
        try:
            response = requests.get(portal_url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Failed to fetch BCAD GIS portal: {exc}") from exc

        soup = BeautifulSoup(response.text, "lxml")
        candidates: list[tuple[int, str]] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            text = anchor.get_text(" ", strip=True)
            if not ZIP_RE.search(href) or not CERTIFIED_LINK_RE.search(text):
                continue
            year_match = YEAR_RE.search(text) or YEAR_RE.search(href)
            if not year_match:
                continue
            candidates.append((int(year_match.group(0)), href))

        if not candidates:
            raise CommandError(
                f"No 'Certified Shapefiles Download' .zip link found at {portal_url}. "
                "The portal layout may have changed."
            )

        candidates.sort(key=lambda pair: pair[0])
        year, href = candidates[-1]
        absolute_url = requests.compat.urljoin(portal_url, href)
        self.stdout.write(f"Latest GIS archive: {absolute_url} (year={year})")
        return absolute_url, year

    # ------------------------------------------------------------------ download / extract

    def _download(self, url: str, destination: Path, *, force: bool, dry_run: bool) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not force:
            self.stdout.write(f"Archive already on disk: {destination}")
            return destination
        if dry_run:
            self.stdout.write(f"[dry-run] would download {url} -> {destination}")
            return destination

        self.stdout.write(f"Downloading {url} -> {destination}")
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
        extract_dir.mkdir(parents=True, exist_ok=True)
        if dry_run:
            self.stdout.write(f"[dry-run] would extract {archive} into {extract_dir}")
            return
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile as exc:
            raise CommandError(f"Archive is not a valid zip: {archive} ({exc})") from exc
        self.stdout.write(f"Extracted into {extract_dir}")

    @staticmethod
    def _find_shapefile(extract_dir: Path) -> Path | None:
        matches = sorted(extract_dir.rglob("*.shp"))
        return matches[0] if matches else None

    # ------------------------------------------------------------------ load

    def _load(self, shapefile_path: Path, tax_year: int, *, dry_run: bool) -> dict[str, int]:
        import geopandas as gpd

        if dry_run:
            self.stdout.write(
                f"[dry-run] would read {shapefile_path}, reproject, and bulk_update "
                f"PropertyAccount rows for tax_year={tax_year}"
            )
            return {"matched": 0, "unmatched": 0}

        self.stdout.write(f"Reading {shapefile_path} ...")
        gdf = gpd.read_file(shapefile_path)
        self.stdout.write(f"Loaded {len(gdf)} parcel features (CRS={gdf.crs})")

        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)

        # NOT "_lat"/"_lon": itertuples() builds a namedtuple, and namedtuple
        # fields can't start with an underscore -- pandas silently renames
        # such columns to positional names ("_0", "_1", ...), which would
        # make getattr(row, "_lat", ...) below always return the default.
        centroids = gdf.geometry.centroid
        gdf["gis_lat"] = centroids.y
        gdf["gis_lon"] = centroids.x

        updates: dict[str, dict] = {}
        for row in gdf.itertuples(index=False):
            raw_prop_id = getattr(row, "PROP_ID", None)
            if raw_prop_id is None or _is_nan(raw_prop_id):
                continue
            prop_id = str(int(raw_prop_id)).zfill(12)

            situs_address = _join_address(
                getattr(row, "situs_num", None),
                getattr(row, "situs_stre", None),
                getattr(row, "situs_st_1", None),
                getattr(row, "situs_st_2", None),
            )
            situs_unit = _clean_str(getattr(row, "situs_unit", None))
            if situs_unit:
                situs_address = f"{situs_address} {situs_unit}".strip()

            # yr_built and yr_blt are NOT simple duplicates: on the real
            # export, both are present and disagree on ~2.4% of parcels
            # (1,503/62,058) -- always with yr_blt >= yr_built, consistent
            # with yr_blt being an "effective" year (reset by a major
            # renovation) rather than true original construction year. Prefer
            # yr_built (also the more complete field: populated on 144 rows
            # where yr_blt is 0, vs. only 1,422 the other way).
            yr_built = _clean_int(getattr(row, "yr_built", None)) or _clean_int(
                getattr(row, "yr_blt", None)
            )

            updates[prop_id] = {
                "situs_address": situs_address,
                # No situs_city/situs_zip: the shapefile has no genuine situs
                # city/zip field, only situs_num/stre/st_1/st_2/unit (street
                # level). addr_city/addr_state/addr_zip are confirmed MAILING
                # fields (see module docstring) -- do not use them here, that
                # would reintroduce the exact mailing/situs conflation this
                # command exists to fix. situs_state is safe to hardcode:
                # every Brazos County parcel is in Texas.
                "situs_state": "TX",
                # total_value/land_value/improvement_value/assessed_value:
                # deliberately NOT sourced here -- see module docstring.
                "state_class": _clean_str(getattr(row, "state_cd", None)),
                "latitude": _clean_decimal(getattr(row, "gis_lat", None)),
                "longitude": _clean_decimal(getattr(row, "gis_lon", None)),
                "living_area": _clean_decimal(getattr(row, "living_are", None)),
                "year_built": yr_built,
                "class_code": _clean_str(getattr(row, "class_cd", None)),
            }

        if not updates:
            logger.warning("No usable PROP_ID rows found in %s", shapefile_path)
            return {"matched": 0, "unmatched": 0}

        # Fetch every PropertyAccount row for this tax_year up front and match
        # in Python, rather than prop_id__in=updates.keys() -- with ~77k
        # shapefile parcels, that generates a single SQL IN clause with tens
        # of thousands of literals, which is a genuine (observed) performance
        # cliff for Postgres query planning. A plain tax_year filter uses the
        # existing (tax_year, prop_id) index and is comparably cheap however
        # many rows come back.
        accounts_by_prop_id = {
            account.prop_id: account
            for account in PropertyAccount.objects.filter(tax_year=tax_year)
        }
        accounts = []
        for prop_id, fields in updates.items():
            account = accounts_by_prop_id.get(prop_id)
            if account is None:
                continue
            for name, value in fields.items():
                setattr(account, name, value)
            accounts.append(account)

        # NOT batch_size=5000: unlike bulk_create (a plain multi-row INSERT),
        # bulk_update generates one UPDATE per batch with a CASE-WHEN
        # expression per column, keyed by a WHERE id IN (...) of batch_size
        # ids -- cost scales with rows_per_batch^2 x column_count. With 14
        # columns, batch_size=5000 was observed to hang for 6+ minutes
        # (verified stuck server-side via pg_stat_activity) and had to be
        # killed. batch_size=200 keeps each statement small.
        PropertyAccount.objects.bulk_update(accounts, PROPERTY_FIELDS_UPDATED, batch_size=200)

        matched = len(accounts)
        unmatched = len(updates) - matched
        logger.info(
            "Updated %d PropertyAccount rows from GIS shapefile (tax_year=%s); "
            "%d shapefile prop_ids had no matching PropertyAccount row",
            matched,
            tax_year,
            unmatched,
        )
        return {"matched": matched, "unmatched": unmatched}

    # ------------------------------------------------------------------ main

    def handle(self, *args, **options):
        force: bool = options["force"]
        skip_download: bool = options["skip_download"]
        skip_extract: bool = options["skip_extract"]
        dry_run: bool = options["dry_run"]
        requested_year: int | None = options.get("year")

        download_dir = Path(settings.BCAD_DOWNLOAD_DIR)
        extract_root = Path(settings.BCAD_EXTRACT_DIR) / "gis"

        if not skip_download:
            url, scraped_year = self._scrape_archive(GIS_PORTAL_URL)
            archive_label = scraped_year
        else:
            url = ""
            archive_label = requested_year or "latest"

        archive = download_dir / f"bcad_gis_{archive_label}.zip"
        extract_dir = extract_root / str(archive_label)

        if not skip_download:
            self._download(url, archive, force=force, dry_run=dry_run)
        elif not archive.exists():
            raise CommandError(
                f"--skip-download set but archive not found: {archive}. "
                "Remove --skip-download or pass --force."
            )

        if not skip_extract:
            self._extract(archive, extract_dir, dry_run=dry_run)

        shapefile_path = self._find_shapefile(extract_dir)
        if shapefile_path is None and not dry_run:
            raise CommandError(f"No .shp file found under {extract_dir}. Did extraction succeed?")

        target_year = requested_year
        if target_year is None:
            latest = (
                PropertyAccount.objects.order_by("-tax_year")
                .values_list("tax_year", flat=True)
                .first()
            )
            if latest is None:
                raise CommandError(
                    "No PropertyAccount rows exist yet -- run load_brazos_cad first, "
                    "or pass --year explicitly."
                )
            target_year = latest

        if dry_run:
            self.stdout.write(
                f"[dry-run] would update PropertyAccount rows for tax_year={target_year}"
            )
            return

        results = self._load(shapefile_path, target_year, dry_run=dry_run)
        self.stdout.write(
            self.style.SUCCESS(
                f"GIS load complete for tax_year={target_year}: "
                f"{results['matched']} PropertyAccount rows updated, "
                f"{results['unmatched']} shapefile parcels had no match."
            )
        )
