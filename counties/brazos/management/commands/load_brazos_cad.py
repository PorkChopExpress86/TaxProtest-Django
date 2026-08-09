"""Custom management command: load Brazos Central Appraisal District (BCAD)
PACS-format certified data into the local database.

Pipeline:
  1. Scrape https://brazoscad.org/certified-data-downloads/ to locate the
     latest certified .zip archive.
  2. Stream-download the archive into BCAD_DOWNLOAD_DIR (idempotent unless
     --force).
  3. Extract the archive to BCAD_EXTRACT_DIR/<year>/ and locate the real
     (timestamp-prefixed) APPRAISAL_*.TXT files.
  4. Parse each fixed-width .TXT file (see counties/brazos/parsers/pacs.py for the
     verified field layout) and load it via Django ORM bulk_create, scoped to
     (and replacing) only that tax_year's existing rows. Row volumes here
     (well under 300k per file) don't need raw COPY.

Only fields with reliable evidence from a real 2025 export are ingested — see
brazos_cad/models.py and counties/brazos/parsers/pacs.py docstrings for exactly
which fields are and aren't populated (e.g. assessed_value/total_value/
state_class are not currently sourced on PropertyAccount; APPRAISAL_ENTITY.TXT
isn't ingested at all, since it's an id/code lookup, not an entity master).

APPRAISAL_ENTITY_INFO.TXT is ingested into counties.harris.models.PropertyJurisdictionExemption
(county="brazos") -- NOT a brazos_cad model. That table is shared with Harris
(see wayfinder ticket #9): counties.harris.tax_impact.calculate_tax_impact() is reused
verbatim across counties once its three backing tables (TaxUnitRate,
PropertyJurisdictionExemption, AssessmentHistory) carry a matching county's
rows. Deletes here are always scoped by county="brazos" to avoid touching
Harris's rows in the same tables.

IMPORTANT ordering dependency: this command's APPRAISAL_INFO.TXT step does a
full delete-then-recreate of ALL PropertyAccount rows for --year (see
_load_file). load_brazos_gis (a separate command) supplements those same
rows afterward with situs address/coordinates/values/state_class/
living_area/year_built/class_code from the GIS shapefile. Re-running THIS
command after load_brazos_gis has run silently wipes those GIS-sourced
fields back to blank/null (confirmed: this happened once during development
and required re-running load_brazos_gis to fix). Always run load_brazos_gis
again after any re-run of this command, or run it in a chain:
    load_brazos_cad --year YYYY && load_brazos_gis --year YYYY

Usage:
  python manage.py load_brazos_cad [--year YYYY] [--force]
      [--skip-download] [--skip-extract] [--skip-ingest] [--dry-run]
"""

from __future__ import annotations

import logging
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from counties.brazos.models import (
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)
from counties.brazos.parsers.pacs import (
    parse_entity_info_line,
    parse_improvement_detail_attr_line,
    parse_improvement_detail_line,
    parse_improvement_info_line,
    parse_info_line,
    parse_land_detail_line,
)
from counties.harris.models import PropertyJurisdictionExemption

logger = logging.getLogger("brazos_cad")


BCAD_PORTAL_URL = "https://brazoscad.org/certified-data-downloads/"
USER_AGENT = "TaxProtest-Django/1.0 (+brazos_cad loader)"
DOWNLOAD_TIMEOUT = 300  # seconds

YEAR_RE = re.compile(r"(19|20)\d{2}")
ZIP_RE = re.compile(r"\.zip$", re.IGNORECASE)

IMPROVEMENT_DETAIL_FILENAME = "APPRAISAL_IMPROVEMENT_DETAIL.TXT"
ENTITY_INFO_FILENAME = "APPRAISAL_ENTITY_INFO.TXT"
IMPROVEMENT_DETAIL_ATTR_FILENAME = "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT"

# Only these three exemption-amount fields are ingested -- see the
# ENTITY_INFO_LAYOUT docstring in parsers/pacs.py for why dv_amt and the
# ab/en/fr/... block are excluded. Order controls exemption_code assignment.
EXEMPTION_AMOUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("hs_amt", "HS"),
    ("ov65_amt", "OV65"),
    ("dp_amt", "DP"),
)

# APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT's attribute_type -> the single
# PropertyBuildingCharacteristic field it maps to. "Plumbing" is handled
# separately (parses into bathrooms + half_baths, not a plain pass-through).
# Anything NOT in this dict and not "Plumbing" is treated as a
# PropertyExtraFeature row instead (fireplaces, patios, carports, pools,
# built-ins, free-text "Other Feature" entries) -- a closed allowlist here
# would silently drop any attribute type BCAD adds in a future export.
WIDE_ATTRIBUTE_FIELDS: dict[str, str] = {
    "Number of Bedrooms": "bedrooms",
    "Number of Rooms": "room_count",
    "Exterior Wall": "exterior_wall",
    "Foundation": "foundation",
    "Roof Covering": "roof_covering",
    "Heating/Cooling": "heating_cooling",
    "Interior Finish": "interior_finish",
    "Construction Style": "construction_style",
    "Flooring": "flooring",
}

# "Plumbing" attribute values are messy real-world free text -- matched
# against real 2025 export data (43,495 rows, 35 distinct value shapes,
# these patterns cover 43,485/43,495 = 99.98%). Order matters: fraction-style
# ("2 1/2") must be checked before plain slash ("2/1") since both use "/".
_BATHROOM_FRACTION_RE = re.compile(r"^(\d+)[\s,-]+1/2\s*(?:EA)?$", re.IGNORECASE)
_BATHROOM_DECIMAL_RE = re.compile(r"^(\d+)\.5$")
_BATHROOM_SLASH_RE = re.compile(r"^(\d+)[/\\](\d+)$")
_BATHROOM_PLAIN_RE = re.compile(r"^(\d+)$")


def _parse_bathrooms(raw: str) -> tuple[Decimal | None, int | None]:
    """Parse a "Plumbing" attribute value into (full_baths, half_baths).
    Returns (None, None) for the small remainder of unparseable free text
    (e.g. "1 3/4") rather than guessing."""
    text = raw.strip()
    match = _BATHROOM_FRACTION_RE.match(text)
    if match:
        return Decimal(match.group(1)), 1
    match = _BATHROOM_DECIMAL_RE.match(text)
    if match:
        return Decimal(match.group(1)), 1
    match = _BATHROOM_SLASH_RE.match(text)
    if match:
        return Decimal(match.group(1)), int(match.group(2))
    match = _BATHROOM_PLAIN_RE.match(text)
    if match:
        return Decimal(match.group(1)), 0
    return None, None


def _attr_int_or_none(raw: str) -> int | None:
    """For "Number of Bedrooms"/"Number of Rooms" -- blank or non-numeric -> None."""
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class IngestSpec:
    """Mapping from a real BCAD .TXT filename to a Django model + its fixed-width parser."""

    filename: str
    model: type
    parse_fn: Callable[[str], dict]


# Ordering matters: APPRAISAL_IMPROVEMENT_INFO.TXT must load before
# APPRAISAL_IMPROVEMENT_DETAIL.TXT (handled separately below, not in this
# tuple) because the year_built rollup updates PropertyImprovement rows that
# must already exist.
INGEST_SPECS: tuple[IngestSpec, ...] = (
    IngestSpec("APPRAISAL_INFO.TXT", PropertyAccount, parse_info_line),
    IngestSpec("APPRAISAL_LAND_DETAIL.TXT", PropertyLand, parse_land_detail_line),
    IngestSpec("APPRAISAL_IMPROVEMENT_INFO.TXT", PropertyImprovement, parse_improvement_info_line),
)

ALL_FILENAMES: tuple[str, ...] = tuple(spec.filename for spec in INGEST_SPECS) + (
    IMPROVEMENT_DETAIL_FILENAME,
    ENTITY_INFO_FILENAME,
    IMPROVEMENT_DETAIL_ATTR_FILENAME,
)


class Command(BaseCommand):
    help = "Scrape, download, extract, and ingest BCAD certified property data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            help="Target tax year (e.g., 2024). If omitted, the latest available is used.",
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
            help="Skip the extraction step (use already-extracted files).",
        )
        parser.add_argument(
            "--skip-ingest",
            action="store_true",
            help="Skip the database COPY step (download + extract only).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log every action without writing to disk or the database.",
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _candidate_year(text: str) -> int | None:
        match = YEAR_RE.search(text)
        return int(match.group(0)) if match else None

    # ------------------------------------------------------------------ scrape

    def _scrape_archive(self, portal_url: str) -> tuple[str, int]:
        """Return (absolute_url, year) for the latest certified archive.

        Heuristic: pick the highest year mentioned in any .zip link's
        text or href; if the year is missing, use the last .zip link on
        the page. Fails loudly if no .zip is found.
        """
        self.stdout.write(f"Scraping BCAD portal: {portal_url}")
        try:
            response = requests.get(portal_url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Failed to fetch BCAD portal: {exc}") from exc

        soup = BeautifulSoup(response.text, "lxml")
        zip_links: list[tuple[int | None, str]] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            text = anchor.get_text(" ", strip=True)
            if not ZIP_RE.search(href):
                continue
            year = self._candidate_year(text) or self._candidate_year(href)
            zip_links.append((year, href))

        if not zip_links:
            raise CommandError(
                f"No .zip archive links found at {portal_url}. "
                "The portal layout may have changed."
            )

        # Prefer the highest year; fall back to the last link if none had a year.
        zip_links.sort(key=lambda pair: (pair[0] is None, pair[0] or 0))
        year, href = zip_links[-1]
        absolute_url = requests.compat.urljoin(portal_url, href)
        self.stdout.write(f"Latest archive: {absolute_url} (year={year or 'unknown'})")
        return absolute_url, year or 0

    # ------------------------------------------------------------------ download

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

    # ------------------------------------------------------------------ extract

    def _extract(self, archive: Path, extract_dir: Path, *, dry_run: bool) -> list[Path]:
        if not archive.exists():
            raise CommandError(f"Archive not found: {archive}")
        extract_dir.mkdir(parents=True, exist_ok=True)

        extracted: list[Path] = []
        try:
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                if dry_run:
                    self.stdout.write(
                        f"[dry-run] would extract {len(names)} files into {extract_dir}"
                    )
                    return [extract_dir / n for n in names]
                zf.extractall(extract_dir)
                for name in names:
                    extracted.append(extract_dir / name)
        except zipfile.BadZipFile as exc:
            raise CommandError(f"Archive is not a valid zip: {archive} ({exc})") from exc

        self.stdout.write(f"Extracted {len(extracted)} files into {extract_dir}")
        return extracted

    @staticmethod
    def _resolve_text_files(extract_dir: Path) -> dict[str, Path]:
        """Map each expected APPRAISAL_*.TXT filename to a real path on disk.

        Real BCAD export filenames carry a "YYYY-MM-DD_HHMMSS_" timestamp
        prefix (e.g. "2025-07-23_002022_APPRAISAL_INFO.TXT"), so match by
        suffix rather than exact name. If a --force re-extraction leaves
        multiple timestamped copies of the same target file in one year's
        directory, ISO-formatted prefixes sort correctly as strings, so
        picking the lexicographically-last match picks the newest.
        """
        out: dict[str, Path] = {}
        for filename in ALL_FILENAMES:
            matches = sorted(extract_dir.rglob(f"*{filename}"), reverse=True)
            if not matches:
                logger.warning("Missing expected file: %s", filename)
                continue
            if len(matches) > 1:
                logger.warning(
                    "Multiple candidates for %s under %s; using the newest: %s",
                    filename,
                    extract_dir,
                    matches[0].name,
                )
            out[filename] = matches[0]
        return out

    # ------------------------------------------------------------------ ingest

    @staticmethod
    def _iter_lines(text_file: Path):
        with text_file.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.strip():
                    yield line

    def _load_file(self, spec: IngestSpec, text_file: Path, tax_year: int, *, dry_run: bool) -> int:
        if not text_file.exists():
            raise CommandError(f"Text file missing: {text_file}")

        if dry_run:
            self.stdout.write(
                f"[dry-run] {spec.filename}: would delete tax_year={tax_year} rows from "
                f"{spec.model._meta.db_table} and bulk_create from {text_file}"
            )
            return 0

        instances = []
        mismatched = 0
        for line in self._iter_lines(text_file):
            fields = spec.parse_fn(line)
            record_year = fields.pop("tax_year", None)
            if record_year is not None and record_year != tax_year:
                mismatched += 1
                continue
            fields["tax_year"] = tax_year
            fields["source_file"] = text_file.name
            instances.append(spec.model(**fields))

        if mismatched:
            logger.warning(
                "%s: skipped %d rows whose in-record tax_year didn't match requested %d",
                spec.filename,
                mismatched,
                tax_year,
            )

        # Scoped to this tax_year (not a blind TRUNCATE) so re-running for one
        # year doesn't destroy other years' already-loaded data.
        spec.model.objects.filter(tax_year=tax_year).delete()
        spec.model.objects.bulk_create(instances, batch_size=5000)
        logger.info(
            "Loaded %s rows into %s (tax_year=%s)",
            len(instances),
            spec.model._meta.db_table,
            tax_year,
        )
        return len(instances)

    def _load_improvement_detail(self, text_file: Path, tax_year: int, *, dry_run: bool) -> int:
        """Load PropertyImprovementDetail rows and roll up year_built onto
        PropertyImprovement (APPRAISAL_IMPROVEMENT_INFO.TXT carries no
        year_built of its own — only per-detail-row values exist, and one
        improvement typically has several detail rows). The representative
        year_built picked per imp_id is the one from its highest-detail_value
        row (the dominant structural component, usually the main area).
        """
        if not text_file.exists():
            raise CommandError(f"Text file missing: {text_file}")

        if dry_run:
            self.stdout.write(
                f"[dry-run] {IMPROVEMENT_DETAIL_FILENAME}: would delete tax_year={tax_year} rows "
                f"from {PropertyImprovementDetail._meta.db_table}, bulk_create, and roll up year_built"
            )
            return 0

        instances = []
        rollup_best_value: dict[str, Decimal] = {}
        rollup_year_built: dict[str, int] = {}
        mismatched = 0

        for line in self._iter_lines(text_file):
            fields = parse_improvement_detail_line(line)
            record_year = fields.pop("tax_year", None)
            if record_year is not None and record_year != tax_year:
                mismatched += 1
                continue
            year_built = fields.pop("year_built", None)
            imp_id = fields["imp_id"]
            detail_value = fields.get("detail_value") or Decimal("0")
            if year_built is not None and (
                imp_id not in rollup_best_value or detail_value > rollup_best_value[imp_id]
            ):
                rollup_best_value[imp_id] = detail_value
                rollup_year_built[imp_id] = year_built
            fields["tax_year"] = tax_year
            fields["source_file"] = text_file.name
            instances.append(PropertyImprovementDetail(**fields))

        if mismatched:
            logger.warning(
                "%s: skipped %d rows whose in-record tax_year didn't match requested %d",
                IMPROVEMENT_DETAIL_FILENAME,
                mismatched,
                tax_year,
            )

        PropertyImprovementDetail.objects.filter(tax_year=tax_year).delete()
        PropertyImprovementDetail.objects.bulk_create(instances, batch_size=5000)
        logger.info(
            "Loaded %s rows into %s (tax_year=%s)",
            len(instances),
            PropertyImprovementDetail._meta.db_table,
            tax_year,
        )

        if rollup_year_built:
            improvements = list(
                PropertyImprovement.objects.filter(
                    tax_year=tax_year, imp_id__in=rollup_year_built.keys()
                )
            )
            for improvement in improvements:
                improvement.year_built = rollup_year_built.get(improvement.imp_id)
            PropertyImprovement.objects.bulk_update(improvements, ["year_built"], batch_size=5000)
            logger.info("Rolled up year_built onto %d PropertyImprovement rows", len(improvements))

        return len(instances)

    def _load_entity_info(self, text_file: Path, tax_year: int, *, dry_run: bool) -> int:
        """Load PropertyJurisdictionExemption rows (shared with Harris, see
        wayfinder ticket #9) from APPRAISAL_ENTITY_INFO.TXT, and roll up
        each property's assessed_value onto PropertyAccount.

        One source line = one (property, taxing entity) association. Always
        emits a "base" row (exemption_code="") carrying that entity's
        taxable/assessed value -- this is what tells tax_impact.py the
        property owes tax to this entity at all, matching how a property
        with no exemption on a given unit is still represented by Harris's
        own import_jur_exemptions data (see data/tests/test_tax_impact.py).
        Plus one additional row per non-zero exemption amount field.

        The assessed_value rollup exists because PropertyAccount.assessed_value
        is NOT sourced from the GIS shapefile (load_brazos_gis) -- verified
        against BCAD's own live property search that the shapefile linked as
        "<year> Certified" is actually a rolling current/preliminary snapshot,
        not a frozen certified-year archive (confirmed via a real property:
        our shapefile-derived value matched the *next* year's page, not the
        target year's). This file's assessed_val, by contrast, is confirmed
        genuinely tax_year-accurate (it's what the exemption-offset
        verification in docs/research/brazos-exemptions.md cross-checked
        against BCAD's published exemption amounts). assessed_val is the same
        across every entity for a given property (confirmed empirically), so
        the first non-null value encountered per prop_id is authoritative,
        not a "pick a winner" tiebreak like the year_built rollup below.
        """
        if not text_file.exists():
            raise CommandError(f"Text file missing: {text_file}")

        if dry_run:
            self.stdout.write(
                f"[dry-run] {ENTITY_INFO_FILENAME}: would delete tax_year={tax_year} "
                f"county=brazos rows from {PropertyJurisdictionExemption._meta.db_table}, "
                f"bulk_create from {text_file}, and roll up assessed_value onto PropertyAccount"
            )
            return 0

        instances: list[PropertyJurisdictionExemption] = []
        assessed_value_by_prop: dict[str, Decimal] = {}
        mismatched = 0

        for line in self._iter_lines(text_file):
            fields = parse_entity_info_line(line)
            record_year = fields.pop("tax_year", None)
            if record_year is not None and record_year != tax_year:
                mismatched += 1
                continue

            prop_id = fields["prop_id"]
            tax_unit_code = fields["tax_unit_code"]
            tax_unit_name = fields["tax_unit_name"]
            taxable_value = fields["taxable_value"]
            assessed_value = fields["assessed_value"]

            if assessed_value is not None and prop_id not in assessed_value_by_prop:
                assessed_value_by_prop[prop_id] = assessed_value

            instances.append(
                PropertyJurisdictionExemption(
                    account_number=prop_id,
                    tax_year=tax_year,
                    county="brazos",
                    tax_unit_code=tax_unit_code,
                    tax_unit_name=tax_unit_name,
                    exemption_code="",
                    taxable_value=taxable_value,
                    assessed_value=assessed_value,
                    source=text_file.name,
                )
            )
            for amount_field, exemption_code in EXEMPTION_AMOUNT_FIELDS:
                amount = fields.get(amount_field)
                if not amount:
                    continue
                instances.append(
                    PropertyJurisdictionExemption(
                        account_number=prop_id,
                        tax_year=tax_year,
                        county="brazos",
                        tax_unit_code=tax_unit_code,
                        tax_unit_name=tax_unit_name,
                        exemption_code=exemption_code,
                        exemption_amount=amount,
                        taxable_value=taxable_value,
                        assessed_value=assessed_value,
                        source=text_file.name,
                    )
                )

        if mismatched:
            logger.warning(
                "%s: skipped %d rows whose in-record tax_year didn't match requested %d",
                ENTITY_INFO_FILENAME,
                mismatched,
                tax_year,
            )

        # county="brazos" is load-bearing here: this table is shared with
        # Harris (wayfinder ticket #9) -- an unscoped delete would also wipe
        # Harris's rows for the same tax_year.
        PropertyJurisdictionExemption.objects.filter(tax_year=tax_year, county="brazos").delete()
        PropertyJurisdictionExemption.objects.bulk_create(instances, batch_size=5000)
        logger.info(
            "Loaded %s rows into %s (tax_year=%s, county=brazos)",
            len(instances),
            PropertyJurisdictionExemption._meta.db_table,
            tax_year,
        )

        if assessed_value_by_prop:
            accounts = list(
                PropertyAccount.objects.filter(
                    tax_year=tax_year, prop_id__in=assessed_value_by_prop.keys()
                )
            )
            for account in accounts:
                account.assessed_value = assessed_value_by_prop.get(account.prop_id)
            PropertyAccount.objects.bulk_update(accounts, ["assessed_value"], batch_size=200)
            logger.info(
                "Rolled up assessed_value onto %d PropertyAccount rows (tax_year=%s)",
                len(accounts),
                tax_year,
            )

        return len(instances)

    def _load_improvement_detail_attr(
        self, text_file: Path, tax_year: int, *, dry_run: bool
    ) -> int:
        """Load PropertyBuildingCharacteristic (wide, aggregated per
        improvement) and PropertyExtraFeature (long, one row per feature)
        from APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT's attribute-type/value
        pairs. See WIDE_ATTRIBUTE_FIELDS for the type->field routing and
        _parse_bathrooms for "Plumbing"'s special-cased free-text parsing.
        """
        if not text_file.exists():
            raise CommandError(f"Text file missing: {text_file}")

        if dry_run:
            self.stdout.write(
                f"[dry-run] {IMPROVEMENT_DETAIL_ATTR_FILENAME}: would delete tax_year={tax_year} "
                f"rows from {PropertyBuildingCharacteristic._meta.db_table} and "
                f"{PropertyExtraFeature._meta.db_table}, then bulk_create from {text_file}"
            )
            return 0

        # (prop_id, imp_id) -> partial field dict. First occurrence of a
        # given field wins (see PropertyBuildingCharacteristic docstring).
        wide_fields: dict[tuple[str, str], dict[str, object]] = {}
        extra_features: list[PropertyExtraFeature] = []
        mismatched = 0
        unparseable_bathrooms = 0

        for line in self._iter_lines(text_file):
            fields = parse_improvement_detail_attr_line(line)
            record_year = fields.pop("tax_year", None)
            if record_year is not None and record_year != tax_year:
                mismatched += 1
                continue

            prop_id = fields["prop_id"]
            imp_id = fields["imp_id"]
            detail_seq = fields["detail_seq"]
            attribute_type = fields["attribute_type"]
            attribute_value = fields["attribute_value"]

            if attribute_type == "Plumbing":
                bathrooms, half_baths = _parse_bathrooms(attribute_value)
                if bathrooms is None:
                    unparseable_bathrooms += 1
                    continue
                key = (prop_id, imp_id)
                row = wide_fields.setdefault(key, {})
                row.setdefault("bathrooms", bathrooms)
                row.setdefault("half_baths", half_baths)
                continue

            model_field = WIDE_ATTRIBUTE_FIELDS.get(attribute_type)
            if model_field is not None:
                key = (prop_id, imp_id)
                row = wide_fields.setdefault(key, {})
                if model_field not in row:
                    value = attribute_value
                    if model_field in ("bedrooms", "room_count"):
                        value = _attr_int_or_none(attribute_value)
                    if value is not None and value != "":
                        row[model_field] = value
                continue

            extra_features.append(
                PropertyExtraFeature(
                    prop_id=prop_id,
                    imp_id=imp_id,
                    tax_year=tax_year,
                    detail_seq=detail_seq,
                    feature_type=attribute_type,
                    feature_value=attribute_value,
                    source_file=text_file.name,
                )
            )

        if mismatched:
            logger.warning(
                "%s: skipped %d rows whose in-record tax_year didn't match requested %d",
                IMPROVEMENT_DETAIL_ATTR_FILENAME,
                mismatched,
                tax_year,
            )
        if unparseable_bathrooms:
            logger.warning(
                "%s: %d 'Plumbing' values didn't match any known format and were left null",
                IMPROVEMENT_DETAIL_ATTR_FILENAME,
                unparseable_bathrooms,
            )

        characteristics = [
            PropertyBuildingCharacteristic(
                prop_id=prop_id,
                imp_id=imp_id,
                tax_year=tax_year,
                source_file=text_file.name,
                **row,
            )
            for (prop_id, imp_id), row in wide_fields.items()
        ]

        PropertyBuildingCharacteristic.objects.filter(tax_year=tax_year).delete()
        PropertyBuildingCharacteristic.objects.bulk_create(characteristics, batch_size=5000)
        PropertyExtraFeature.objects.filter(tax_year=tax_year).delete()
        PropertyExtraFeature.objects.bulk_create(extra_features, batch_size=5000)

        logger.info(
            "Loaded %s rows into %s and %s rows into %s (tax_year=%s)",
            len(characteristics),
            PropertyBuildingCharacteristic._meta.db_table,
            len(extra_features),
            PropertyExtraFeature._meta.db_table,
            tax_year,
        )
        return len(characteristics) + len(extra_features)

    def _ingest_all(
        self, text_files: dict[str, Path], tax_year: int, *, dry_run: bool
    ) -> dict[str, int]:
        results: dict[str, int] = {}
        for spec in INGEST_SPECS:
            path = text_files.get(spec.filename)
            if path is None:
                results[spec.filename] = 0
                continue
            results[spec.filename] = self._load_file(spec, path, tax_year, dry_run=dry_run)

        detail_path = text_files.get(IMPROVEMENT_DETAIL_FILENAME)
        if detail_path is None:
            results[IMPROVEMENT_DETAIL_FILENAME] = 0
        else:
            results[IMPROVEMENT_DETAIL_FILENAME] = self._load_improvement_detail(
                detail_path, tax_year, dry_run=dry_run
            )

        entity_info_path = text_files.get(ENTITY_INFO_FILENAME)
        if entity_info_path is None:
            results[ENTITY_INFO_FILENAME] = 0
        else:
            results[ENTITY_INFO_FILENAME] = self._load_entity_info(
                entity_info_path, tax_year, dry_run=dry_run
            )

        attr_path = text_files.get(IMPROVEMENT_DETAIL_ATTR_FILENAME)
        if attr_path is None:
            results[IMPROVEMENT_DETAIL_ATTR_FILENAME] = 0
        else:
            results[IMPROVEMENT_DETAIL_ATTR_FILENAME] = self._load_improvement_detail_attr(
                attr_path, tax_year, dry_run=dry_run
            )
        return results

    # ------------------------------------------------------------------ main

    def handle(self, *args, **options):
        force: bool = options["force"]
        skip_download: bool = options["skip_download"]
        skip_extract: bool = options["skip_extract"]
        skip_ingest: bool = options["skip_ingest"]
        dry_run: bool = options["dry_run"]
        requested_year: int | None = options.get("year")

        download_dir: Path = Path(settings.BCAD_DOWNLOAD_DIR)
        extract_root: Path = Path(settings.BCAD_EXTRACT_DIR)

        if not skip_download:
            url, year = self._scrape_archive(BCAD_PORTAL_URL)
            if requested_year and year and year != requested_year:
                self.stdout.write(
                    self.style.WARNING(
                        f"Requested year {requested_year} but latest on portal is {year}. "
                        f"Using latest ({year})."
                    )
                )
            target_year = requested_year or year or 0
            if not target_year:
                raise CommandError(
                    "Could not determine a tax year for the archive. Pass --year YYYY."
                )
        else:
            if requested_year:
                target_year = requested_year
            elif extract_root.exists():
                # Best-effort: take the most recent year subdirectory.
                years = [int(p.name) for p in extract_root.iterdir() if p.name.isdigit()]
                target_year = max(years) if years else 0
            else:
                target_year = 0
            url = ""

        if not target_year:
            raise CommandError(
                "Cannot determine target year; use --year when --skip-download is set."
            )

        archive = download_dir / f"bcad_certified_{target_year}.zip"
        extract_dir = extract_root / str(target_year)

        if not skip_download:
            self._download(url, archive, force=force, dry_run=dry_run)
        elif not archive.exists():
            raise CommandError(
                f"--skip-download set but archive not found: {archive}. "
                "Remove --skip-download or pass --force."
            )

        text_files: dict[str, Path] = {}
        if not skip_extract:
            self._extract(archive, extract_dir, dry_run=dry_run)
        text_files = self._resolve_text_files(extract_dir)
        if not text_files and not dry_run:
            raise CommandError(
                f"No APPRAISAL_*.TXT files found under {extract_dir}. " "Did extraction succeed?"
            )

        if not skip_ingest:
            results = self._ingest_all(text_files, target_year, dry_run=dry_run)
            self.stdout.write(self.style.SUCCESS("Ingest complete:"))
            for filename, count in results.items():
                self.stdout.write(f"  {filename}: {count} rows")
        else:
            self.stdout.write("Skipped ingest (--skip-ingest).")
