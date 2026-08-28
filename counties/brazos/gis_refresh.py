"""GIS source stage for a Brazos annual property refresh.

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

This stage UPDATES existing PropertyAccount rows (via bulk_update, joined
on prop_id + tax_year) rather than the delete-then-recreate pattern
load_brazos_cad uses for the fixed-width files -- PropertyAccount rows must
already exist (from load_brazos_cad) before this command has anything to
join against. The production entry point is ``refresh_brazos_annual``, which
coordinates this enrichment after the CAD rebuild in one transaction. This
low-level command is a targeted recovery adapter and must be run after any
direct use of ``load_brazos_cad`` for the same year, because its
APPRAISAL_INFO.TXT step fully recreates PropertyAccount rows.

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

"""

from __future__ import annotations

import logging
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management.base import CommandError

from counties.brazos.annual_refresh import RefreshOptions, StagePreparation, StageResult
from counties.brazos.models import PropertyAccount
from counties.brazos.portal import USER_AGENT, download_archive, extract_zip
from counties.brazos.stage_reporting import SilentStageReporter, StageReporter

logger = logging.getLogger("brazos_cad")

GIS_PORTAL_URL = "https://brazoscad.org/tax-information/gis/"

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
    "coordinate_source",
    "coordinate_source_year",
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


def _clean_decimal(value: Any):
    if value is None or _is_nan(value):
        return None
    return value


def _clean_int(value: Any) -> int | None:
    if value is None or _is_nan(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_prop_id(value: object) -> str | None:
    """Return BCAD's canonical 12-digit property identifier, if usable."""
    normalized = _clean_int(value)
    if normalized is None:
        return None
    return str(normalized).zfill(12)


@dataclass(frozen=True)
class GisSourcePayload:
    shapefile_path: Path | None
    extract_dir: Path


class GisRefreshStage:
    """Prepare, persist, and clean up the GIS source for one Brazos year."""

    name = "gis"

    def __init__(self, reporter: StageReporter | None = None):
        self._reporter = reporter or SilentStageReporter()

    @property
    def stdout(self):
        return self._reporter.stdout

    @property
    def style(self):
        return self._reporter.style

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
        return download_archive(
            url, destination, force=force, dry_run=dry_run, log=self.stdout.write
        )

    def _extract(self, archive: Path, extract_dir: Path, *, dry_run: bool) -> None:
        extract_zip(archive, extract_dir, dry_run=dry_run, log=self.stdout.write)

    @staticmethod
    def _find_shapefile(extract_dir: Path) -> Path | None:
        matches = sorted(extract_dir.rglob("*.shp"))
        return matches[0] if matches else None

    # ------------------------------------------------------------------ load

    def _load(
        self,
        shapefile_path: Path,
        tax_year: int,
        *,
        dry_run: bool,
        source_year: int | None = None,
    ) -> dict[str, int]:
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

        # Centroid first, reprojection second. A centroid is a planar
        # calculation, so it belongs in the source projected CRS (EPSG:2277,
        # US survey feet) -- running it on EPSG:4326 degrees is what makes
        # geopandas warn "Geometry is in a geographic CRS". The positional
        # difference is sub-metre on parcel-sized polygons, but this order is
        # the correct one and it reprojects N points instead of N polygons.
        centroids = gdf.geometry.centroid
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            centroids = centroids.to_crs(epsg=4326)

        # NOT "_lat"/"_lon": itertuples() builds a namedtuple, and namedtuple
        # fields can't start with an underscore -- pandas silently renames
        # such columns to positional names ("_0", "_1", ...), which would
        # make getattr(row, "_lat", ...) below always return the default.
        gdf["gis_lat"] = centroids.y
        gdf["gis_lon"] = centroids.x

        updates: dict[str, dict] = {}
        for row in gdf.itertuples(index=False):
            raw_prop_id = getattr(row, "PROP_ID", None)
            if raw_prop_id is None or _is_nan(raw_prop_id):
                continue
            prop_id = normalize_prop_id(raw_prop_id)
            if prop_id is None:
                continue

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

            fields = {
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
            if source_year is not None:
                fields["coordinate_source"] = "bcad-certified-gis"
                fields["coordinate_source_year"] = source_year
            updates[prop_id] = fields

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

    @staticmethod
    def _offline_archive_label(download_dir: Path, extract_root: Path) -> int | None:
        """Newest year already on disk, for when --skip-download blocks the scrape.

        The scrape is normally what supplies the year the archive is named
        after (``bcad_gis_<year>.zip``), so with ``--skip-download`` and no
        explicit ``--year`` the year has to be recovered from what a previous
        run left behind: an extracted ``gis/<year>/`` directory, or the
        downloaded archive itself. Returns ``None`` when neither exists.
        """
        years: set[int] = set()
        if extract_root.is_dir():
            years.update(
                int(child.name)
                for child in extract_root.iterdir()
                if child.is_dir() and child.name.isdigit()
            )
        if download_dir.is_dir():
            for archive in download_dir.glob("bcad_gis_*.zip"):
                label = archive.stem.removeprefix("bcad_gis_")
                if label.isdigit():
                    years.add(int(label))
        return max(years) if years else None

    @classmethod
    def _selected_offline_source_year(
        cls,
        requested_year: int | None,
        download_dir: Path,
        extract_root: Path,
    ) -> int | None:
        """Prefer a retained requested source; otherwise expose the actual newest one."""
        if requested_year is not None:
            archive = download_dir / f"bcad_gis_{requested_year}.zip"
            extracted = extract_root / str(requested_year)
            if archive.exists() or extracted.is_dir():
                return requested_year
        return cls._offline_archive_label(download_dir, extract_root)

    def prepare(self, options: RefreshOptions) -> StagePreparation:
        """Stage a parcel shapefile without changing database rows."""
        download_dir = Path(settings.BCAD_DOWNLOAD_DIR)
        extract_root = Path(settings.BCAD_EXTRACT_DIR) / "gis"

        if options.skip_download:
            source_year = self._selected_offline_source_year(
                options.source_year or options.tax_year, download_dir, extract_root
            )
            if source_year is None:
                raise CommandError(
                    f"--skip-download set but no BCAD GIS archive or extracted parcel "
                    f"directory was found under {download_dir} or {extract_root}. "
                    "Drop --skip-download so the archive can be fetched, or pass --year "
                    "to name one explicitly."
                )
            url = ""
        else:
            url, source_year = self._scrape_archive(GIS_PORTAL_URL)
            if options.source_year is not None and source_year != options.source_year:
                raise CommandError(
                    f"BCAD GIS portal selected source year {source_year}, not requested "
                    f"source year {options.source_year}."
                )

        archive = download_dir / f"bcad_gis_{source_year}.zip"
        extract_dir = extract_root / str(source_year)
        if options.skip_download:
            if not archive.exists() and not options.skip_extract:
                raise CommandError(
                    f"--skip-download set but archive not found: {archive}. "
                    "Drop --skip-download so it can be fetched, or pass --year to name "
                    "an archive already on disk."
                )
        else:
            self._download(url, archive, force=options.force, dry_run=options.dry_run)

        if options.dry_run:
            if not options.skip_extract:
                self.stdout.write(f"[dry-run] would extract {archive} -> {extract_dir}")
            shapefile_path = None
        else:
            if not options.skip_extract:
                self._extract(archive, extract_dir, dry_run=False)
            shapefile_path = self._find_shapefile(extract_dir)
            if shapefile_path is None:
                raise CommandError(
                    f"No .shp file found under {extract_dir}. Did extraction succeed?"
                )

        target_year = options.tax_year
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

        return StagePreparation(
            name=self.name,
            source_year=source_year,
            target_year=target_year,
            payload=GisSourcePayload(shapefile_path=shapefile_path, extract_dir=extract_dir),
            cleanup_paths=(extract_dir,),
        )

    def persist(self, preparation: StagePreparation) -> StageResult:
        """Enrich the target year's CAD rows inside the caller's transaction."""
        payload = preparation.payload
        if not isinstance(payload, GisSourcePayload):
            raise TypeError("GIS stage received a preparation from another adapter")
        if payload.shapefile_path is None:
            raise CommandError("No GIS shapefile was prepared for persistence.")

        results = self._load(
            payload.shapefile_path,
            preparation.target_year,
            dry_run=False,
            source_year=preparation.source_year,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"GIS load complete for tax_year={preparation.target_year}: "
                f"{results['matched']} PropertyAccount rows updated, "
                f"{results['unmatched']} shapefile parcels had no match."
            )
        )
        return StageResult(name=self.name, metrics=results)

    def cleanup(self, preparation: StagePreparation) -> None:
        """Remove GIS extraction output after a completed refresh."""
        payload = preparation.payload
        if not isinstance(payload, GisSourcePayload):
            raise TypeError("GIS stage received a preparation from another adapter")
        if payload.extract_dir.exists():
            shutil.rmtree(payload.extract_dir, ignore_errors=True)
            self.stdout.write(self.style.SUCCESS("Cleaned up GIS extracted files."))

    def run(self, options: RefreshOptions) -> StageResult:
        """Run this source stage outside the annual-refresh coordinator."""
        preparation = self.prepare(options)
        if options.dry_run:
            self.stdout.write(
                f"[dry-run] GIS source staged for tax_year={preparation.target_year}; "
                "no database rows were changed."
            )
            return StageResult(name=self.name, metrics={})

        result = self.persist(preparation)
        if not options.keep_extracted and not options.skip_extract:
            self.cleanup(preparation)
        return result
