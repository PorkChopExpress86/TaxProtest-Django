"""Attach parcel coordinates to Brazos CAD accounts from the district's shapefile.

The certified appraisal roll carries no spatial data at all, so accounts loaded by
`load_brazos_cad` have no location until this runs. Brazos CAD publishes parcel
boundaries separately, as an ESRI shapefile per certified year:
https://brazoscad.org/tax-information/gis/

Pipeline
--------
1. Scrape the GIS page for the shapefile archives and pick the year (or --url).
2. Download and extract, reusing the same staging directory as the roll loader.
3. Read only PROP_ID and geometry, reproject to WGS84 and take a representative
   point per parcel.
4. COPY those into an UNLOGGED staging table and apply them with a single
   UPDATE ... FROM against the requested tax year.

Coverage
--------
The shapefile holds ~77k parcels against ~149k accounts, which looks like half the
data is missing. It is not: only real property (prop_type_cd 'R') has a boundary.
Mineral, personal-property and mobile-home accounts have no parcel by nature.
Measured against the 2025 roll, real property matches at 94.5%.

Why representative_point and not centroid
-----------------------------------------
`centroid` is the centre of mass, which falls *outside* L-shaped, crescent and
river-front parcels — common along the Brazos and Navasota rivers. A point that
lands on a neighbouring parcel would quietly corrupt distance-based comparables.
`representative_point()` is guaranteed to lie within the polygon.

Usage
-----
    python manage.py load_brazos_gis                 # latest year
    python manage.py load_brazos_gis --year 2024
    python manage.py load_brazos_gis --list
    python manage.py load_brazos_gis --dry-run
"""

from __future__ import annotations

import os
import re
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from data.brazos_copy import CHUNK_BYTES, copy_into, encode_copy_row
from data.models import PropertyAccount

GIS_PORTAL_URL = "https://brazoscad.org/tax-information/gis/"
USER_AGENT = "TaxProtest-Django/1.0 (+https://github.com/PorkChopExpress86/TaxProtest-Django)"

DOWNLOAD_TIMEOUT = 60
DOWNLOAD_RETRIES = 3
WGS84 = 4326

_YEAR_RE = re.compile(r"(20\d{2})")
# "2025 Certified Shapefiles Download", "Brazos County 2021 Certified Shapefiles"
_CERTIFIED_RE = re.compile(r"certified", re.I)


class Command(BaseCommand):
    help = "Load Brazos CAD parcel coordinates onto PropertyAccount rows."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, help="Tax year to attach coordinates to.")
        parser.add_argument("--list", action="store_true", help="List available shapefiles.")
        parser.add_argument("--url", help="Explicit shapefile .zip URL.")
        parser.add_argument("--archive", help="Path to an already-downloaded .zip.")
        parser.add_argument("--data-dir", help="Staging directory.")
        parser.add_argument(
            "--shapefile-year",
            type=int,
            help="Use this year's shapefile for a different tax year. Parcel geometry "
            "changes far more slowly than value, so borrowing a nearby year is usually "
            "better than having no coordinates at all.",
        )
        parser.add_argument("--skip-download", action="store_true", help="Use existing files only.")
        parser.add_argument(
            "--cleanup",
            action="store_true",
            help="Delete the archive and extract after a successful load.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report coverage without writing."
        )

    # -- entry point --------------------------------------------------------

    def handle(self, *args, **options):
        self.verbosity = options["verbosity"]
        self.dry_run = options["dry_run"]

        if connection.vendor != "postgresql":
            raise CommandError(
                f"load_brazos_gis requires PostgreSQL (COPY); current backend is "
                f"'{connection.vendor}'."
            )
        try:
            import geopandas  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dependency is in requirements
            raise CommandError(
                "geopandas is required. It ships in requirements.txt; rebuild the image."
            ) from exc

        if options["list"]:
            for year, url in sorted(self._fetch_portal().items(), reverse=True):
                self.stdout.write(f"  {year}  {url}")
            return

        data_dir = self._resolve_data_dir(options["data_dir"])
        archive, year = self._obtain_archive(options, data_dir)
        tax_year = options["year"] or year

        extract_dir = data_dir / "gis" / str(year)
        shapefile = self._extract(archive, extract_dir)

        points = self._read_points(shapefile)
        updated, matched = self._apply(points, tax_year)

        if options["cleanup"] and not self.dry_run:
            self._cleanup(archive, extract_dir, data_dir, bool(options["archive"]))

        self._report(tax_year, updated, matched)

    # -- portal -------------------------------------------------------------

    def _fetch_portal(self) -> dict[int, str]:
        """Scrape the GIS page into {year: shapefile_url}."""
        self._log(f"Scraping {GIS_PORTAL_URL}")
        try:
            response = requests.get(
                GIS_PORTAL_URL, timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Could not reach the BCAD GIS page: {exc}") from exc

        soup = BeautifulSoup(response.text, "html.parser")
        archives: dict[int, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href.lower().endswith(".zip"):
                continue
            text = anchor.get_text(" ", strip=True)
            # Skip the map books, which are scanned images rather than parcels.
            if "map book" in text.lower():
                continue
            if not _CERTIFIED_RE.search(text):
                continue
            match = _YEAR_RE.search(text)
            if not match:
                continue
            archives.setdefault(int(match.group(1)), requests.compat.urljoin(GIS_PORTAL_URL, href))

        if not archives:
            raise CommandError(
                "No certified shapefiles found on the BCAD GIS page — layout may have changed."
            )
        return archives

    # -- acquisition --------------------------------------------------------

    def _resolve_data_dir(self, override: str | None) -> Path:
        raw = override or os.environ.get("BRAZOS_DATA_DIR")
        path = Path(raw) if raw else Path(settings.BASE_DIR) / "data" / "cad_downloads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _obtain_archive(self, options: dict, data_dir: Path) -> tuple[Path, int]:
        if options["archive"]:
            path = Path(options["archive"])
            if not path.exists():
                raise CommandError(f"Archive not found: {path}")
            return path, options["shapefile_year"] or options["year"] or self._infer_year(path.name)

        wanted = options["shapefile_year"] or options["year"]
        if options["url"]:
            url, year = options["url"], (wanted or self._infer_year(options["url"]))
        else:
            archives = self._fetch_portal()
            year = wanted or max(archives)
            if year not in archives:
                available = ", ".join(str(y) for y in sorted(archives, reverse=True))
                raise CommandError(
                    f"No shapefile published for {year}. Available: {available}. "
                    f"Use --shapefile-year to borrow a nearby year's geometry."
                )
            url = archives[year]

        target = data_dir / f"brazos_{year}_parcels.zip"
        if options["skip_download"]:
            if not target.exists():
                raise CommandError(f"--skip-download given but {target} does not exist.")
        else:
            self._download(url, target)
        return target, year

    def _infer_year(self, name: str) -> int:
        match = _YEAR_RE.search(name)
        if not match:
            raise CommandError(f"Could not infer a year from '{name}'; pass --year.")
        return int(match.group(1))

    def _download(self, url: str, target: Path) -> None:
        expected = self._remote_size(url)
        if target.exists() and expected and target.stat().st_size == expected:
            self._log(f"Archive already complete ({expected:,} bytes) — skipping download.")
            return

        partial = target.with_suffix(".zip.part")
        last_error: Exception | None = None
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            try:
                self._log(f"Downloading {url} [attempt {attempt}/{DOWNLOAD_RETRIES}]")
                with requests.get(
                    url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": USER_AGENT}
                ) as response:
                    response.raise_for_status()
                    written = 0
                    with open(partial, "wb") as fh:
                        for block in response.iter_content(CHUNK_BYTES):
                            fh.write(block)
                            written += len(block)
                if expected and written != expected:
                    raise OSError(f"size mismatch: got {written:,}, expected {expected:,}")
                partial.replace(target)
                self._log(f"Saved {target} ({written:,} bytes)")
                return
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                time.sleep(2**attempt)
        raise CommandError(f"Download failed after {DOWNLOAD_RETRIES} attempts: {last_error}")

    def _remote_size(self, url: str) -> int | None:
        try:
            head = requests.head(
                url,
                timeout=DOWNLOAD_TIMEOUT,
                allow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            head.raise_for_status()
            return int(head.headers["Content-Length"])
        except (requests.RequestException, KeyError, ValueError):
            return None

    def _extract(self, archive: Path, extract_dir: Path) -> Path:
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            zf = zipfile.ZipFile(archive)
        except zipfile.BadZipFile as exc:
            raise CommandError(f"{archive} is not a readable zip ({exc}).") from exc

        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                destination = extract_dir / Path(info.filename).name
                if destination.exists() and destination.stat().st_size == info.file_size:
                    continue
                self._log(f"  extracting {destination.name} ({info.file_size:,} bytes)")
                with zf.open(info) as src, open(destination, "wb") as dst:
                    while block := src.read(CHUNK_BYTES):
                        dst.write(block)

        return self._select_parcel_layer(extract_dir)

    def _select_parcel_layer(self, extract_dir: Path) -> Path:
        """Pick the parcel-boundary layer out of the archive.

        Year to year this is anything from a single `Parcels_*.shp` to a 33-layer
        GIS package containing roads, creeks, subdivisions and text annotations.
        Names are no guide — the 2024 package has a `Parcel_ID.shp` that is pure
        map lettering, and its real boundaries live in
        `Public_Parcel_Boundary_certified.shp`. So select on content: polygons
        carrying a property-id attribute, and among those the largest layer.
        """
        from pyogrio import read_info

        shapefiles = sorted(extract_dir.glob("*.shp"))
        if not shapefiles:
            raise CommandError(f"No .shp found in {extract_dir}")

        candidates: list[tuple[int, Path, str]] = []
        rejected: list[str] = []
        for path in shapefiles:
            try:
                info = read_info(path)
            except Exception as exc:  # noqa: BLE001 - a bad layer must not stop selection
                rejected.append(f"{path.name} (unreadable: {exc})")
                continue
            column = self._prop_id_column(list(info["fields"]))
            geometry = (info.get("geometry_type") or "").lower()
            if column and "polygon" in geometry:
                candidates.append((int(info["features"]), path, column))
            else:
                rejected.append(path.name)

        if not candidates:
            raise CommandError(
                f"No polygon layer with a property-id column in {extract_dir}. "
                f"Inspected: {', '.join(p.name for p in shapefiles)}"
            )

        features, path, column = max(candidates, key=lambda c: c[0])
        if len(shapefiles) > 1:
            self._log(
                f"  {len(shapefiles)} layers present; using {path.name} "
                f"({features:,} polygons, id column {column!r})"
            )
        return path

    @staticmethod
    def _prop_id_columns(fields: list[str]) -> list[str]:
        """All plausible property-id attributes, whose spelling varies by year.

        More than one usually appears and they are not interchangeable: the 2024
        layer carries both `PROP_ID` holding the string 'R22549' and `PROP_ID1`
        holding the integer 22549. Which is usable is decided from the data in
        `_read_points`, not from the name.
        """
        wanted = ("PROPID", "PROPID1", "PROPERTYID", "PROPIDDBF", "PROPID2")
        return [f for f in fields if f.upper().replace("_", "") in wanted]

    @classmethod
    def _prop_id_column(cls, fields: list[str]) -> str | None:
        """First plausible property-id attribute, for layer selection."""
        columns = cls._prop_id_columns(fields)
        return columns[0] if columns else None

    @staticmethod
    def _parse_prop_id(value) -> int | None:
        """Coerce a shapefile id to an integer prop_id.

        Handles both the bare integer and the 'R'-prefixed string form the
        district uses in different years.
        """
        if value is None:
            return None
        if isinstance(value, (int,)) and not isinstance(value, bool):
            return int(value) or None
        digits = re.sub(r"\D", "", str(value))
        if not digits:
            return None
        parsed = int(digits)
        return parsed or None

    # -- geometry -----------------------------------------------------------

    def _read_points(self, shapefile: Path) -> list[tuple[int, float, float, float]]:
        """Return (prop_id, latitude, longitude, area_sqft) per parcel."""
        import geopandas as gpd
        from pyogrio import read_info

        started = time.monotonic()
        self.stdout.write(f"Reading {shapefile.name}")

        columns = self._prop_id_columns(list(read_info(shapefile)["fields"]))
        if not columns:
            raise CommandError(f"{shapefile.name} has no property-id column.")

        # The .dbf carries 40+ char(254) columns and runs to hundreds of MB.
        # Requesting only the id columns keeps this to seconds, not minutes.
        gdf = gpd.read_file(shapefile, columns=columns, engine="pyogrio")
        self._log(f"  {len(gdf):,} parcels, crs={gdf.crs.name if gdf.crs else 'unknown'}")

        # Pick the id column on evidence rather than on name: score each by how
        # many distinct usable ids it yields and take the best.
        parsed_by_column = {column: gdf[column].map(self._parse_prop_id) for column in columns}
        column = max(columns, key=lambda c: parsed_by_column[c].nunique())
        if len(columns) > 1:
            scores = ", ".join(f"{c}={parsed_by_column[c].nunique():,}" for c in columns)
            self._log(f"  id columns: {scores} — using {column!r}")
        gdf = gdf.assign(PROP_ID=parsed_by_column[column])

        gdf = gdf[gdf.geometry.notna()]

        invalid = ~gdf.geometry.is_valid
        if count := int(invalid.sum()):
            # Self-intersecting rings; buffer(0) rebuilds them without moving the
            # boundary, and an invalid polygon has no reliable interior point.
            self._log(f"  repairing {count} invalid geometries")
            gdf.loc[invalid, "geometry"] = gdf.loc[invalid, "geometry"].buffer(0)
            gdf = gdf[~gdf.geometry.is_empty]

        # Area in the source projection (Texas Central, US survey feet) before
        # reprojecting — computing area in degrees would be meaningless.
        area_sqft = gdf.geometry.area if gdf.crs and gdf.crs.axis_info else None

        if gdf.crs and gdf.crs.to_epsg() != WGS84:
            gdf = gdf.to_crs(epsg=WGS84)
        elif not gdf.crs:
            raise CommandError(
                f"{shapefile.name} has no projection (.prj missing); cannot map to lat/lon."
            )

        points = gdf.geometry.representative_point()

        rows: list[tuple[int, float, float, float]] = []
        seen: set[int] = set()
        for index, prop_id in gdf["PROP_ID"].items():
            if prop_id is None or prop_id != prop_id:  # None or NaN
                continue
            pid = int(prop_id)
            # Multi-part parcels repeat a PROP_ID; the first ring wins so the
            # result is deterministic across runs.
            if pid in seen:
                continue
            seen.add(pid)
            point = points.loc[index]
            area = float(area_sqft.loc[index]) if area_sqft is not None else None
            rows.append((pid, point.y, point.x, area))

        self._log(
            f"  {len(rows):,} unique parcels in {time.monotonic() - started:,.1f}s "
            f"({len(gdf) - len(rows):,} duplicate PROP_IDs collapsed)"
        )
        return rows

    # -- database -----------------------------------------------------------

    def _apply(
        self, points: list[tuple[int, float, float, float]], tax_year: int
    ) -> tuple[int, int]:
        """COPY points into staging and update the year in one statement."""
        total_accounts = PropertyAccount.objects.filter(tax_year=tax_year).count()
        if not total_accounts:
            raise CommandError(
                f"No PropertyAccount rows for {tax_year}. Run load_brazos_cad first."
            )

        if self.dry_run:
            shp_ids = {pid for pid, _lat, _lon, _area in points}
            db_ids = set(
                PropertyAccount.objects.filter(tax_year=tax_year).values_list("prop_id", flat=True)
            )
            matched = len(shp_ids & db_ids)
            self._log(f"  dry run: {matched:,} of {total_accounts:,} accounts would be located")
            return 0, matched

        staging = f"stg_brazos_gis_{tax_year}"
        table = PropertyAccount._meta.db_table

        def rows() -> Iterator[str]:
            for pid, lat, lon, area in points:
                yield encode_copy_row(
                    [
                        str(pid),
                        f"{lat:.7f}",
                        f"{lon:.7f}",
                        None if area is None else f"{area:.2f}",
                    ]
                )

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f'DROP TABLE IF EXISTS "{staging}"')
                cursor.execute(
                    f'CREATE UNLOGGED TABLE "{staging}" '
                    f"(prop_id bigint primary key, latitude numeric, longitude numeric, "
                    f"area_sqft numeric)"
                )
                copy_into(
                    cursor,
                    f'COPY "{staging}" (prop_id, latitude, longitude, area_sqft) '
                    f"FROM STDIN WITH (FORMAT text)",
                    rows(),
                )
                cursor.execute(
                    f'UPDATE "{table}" a SET latitude = s.latitude, longitude = s.longitude, '
                    f"parcel_area_sqft = s.area_sqft, updated_at = now() "
                    f'FROM "{staging}" s WHERE a.prop_id = s.prop_id AND a.tax_year = %s',
                    [tax_year],
                )
                updated = cursor.rowcount
                cursor.execute(f'DROP TABLE IF EXISTS "{staging}"')

        return updated, updated

    def _report(self, tax_year: int, updated: int, matched: int) -> None:
        base = PropertyAccount.objects.filter(tax_year=tax_year)
        real = base.filter(prop_type_cd="R")
        real_total = real.count()
        real_located = real.filter(latitude__isnull=False).count() if not self.dry_run else matched

        verb = "would locate" if self.dry_run else "located"
        self.stdout.write(
            self.style.SUCCESS(
                f"Brazos GIS {tax_year}: {verb} {matched:,} of {base.count():,} accounts."
            )
        )
        if real_total and not self.dry_run:
            pct = real_located / real_total * 100
            self.stdout.write(
                f"  real property (type R): {real_located:,}/{real_total:,} ({pct:.1f}%)"
            )
            # Mineral, personal-property and mobile-home accounts have no parcel
            # boundary, so they are expected to stay NULL.
            self.stdout.write("  other types have no parcel boundary and stay unlocated.")
            if pct < 80:
                self.stderr.write(
                    self.style.WARNING(
                        f"  ! only {pct:.1f}% of real property matched — the shapefile year "
                        f"may not line up with the roll year."
                    )
                )

    def _cleanup(
        self, archive: Path, extract_dir: Path, data_dir: Path, user_supplied: bool
    ) -> None:
        freed = 0
        if extract_dir.exists():
            for child in extract_dir.iterdir():
                if child.is_file():
                    freed += child.stat().st_size
                    child.unlink(missing_ok=True)
            extract_dir.rmdir()
        if (
            not user_supplied
            and archive.exists()
            and data_dir.resolve() in archive.resolve().parents
        ):
            freed += archive.stat().st_size
            archive.unlink(missing_ok=True)
        if freed:
            self._log(f"Cleaned up staged files, freed {freed / 1e6:,.1f} MB.")

    def _log(self, message: str) -> None:
        if self.verbosity >= 1:
            self.stdout.write(message)
