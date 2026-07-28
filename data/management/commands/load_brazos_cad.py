"""Download, extract and bulk-load the Brazos CAD certified appraisal roll.

Pipeline
--------
1. Scrape https://brazoscad.org/certified-data-downloads/ for the certified
   ``.zip`` archives and pick the latest year (or ``--year``).
2. Download it to the bind-mounted staging directory, resuming a partial
   transfer and skipping the download entirely if a complete copy is present.
3. Extract the fixed-width ``.TXT`` members needed by data/brazos_layouts.py.
4. Stream each file into PostgreSQL with ``COPY`` and merge it into the target
   table with ``INSERT ... ON CONFLICT DO UPDATE``.
5. Delete the staged archive and extracts, which is ~1.6 GB per year once the
   rows are safely in the database. Suppressed on failure, so a retry is cheap.

Why COPY and not the ORM
------------------------
The 2025 export is a 1.3 GB property file and a 180 MB improvement-detail file.
``bulk_create()`` builds every model instance in memory first and would be OOM-
killed under the container's memory limit long before it finished. Instead rows
are sliced, converted and pushed straight into ``COPY ... FROM STDIN`` through a
generator-backed file object, so peak memory is one chunk of text regardless of
file size, and PostgreSQL does the insert at its native bulk rate.

Loading goes via an UNLOGGED staging table rather than directly into the target:
COPY cannot express upsert semantics, and the merge step is what makes re-running
an import idempotent instead of a unique-violation crash.

Usage
-----
    python manage.py load_brazos_cad                    # latest year, full load
    python manage.py load_brazos_cad --year 2024
    python manage.py load_brazos_cad --list             # show available years
    python manage.py load_brazos_cad --only PropertyAccount
    python manage.py load_brazos_cad --archive /path/to/export.zip
    python manage.py load_brazos_cad --dry-run          # parse, don't write
    python manage.py load_brazos_cad --keep-archive     # keep the .zip for a re-run
"""

from __future__ import annotations

import os
import re
import time
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from data.brazos_copy import CHUNK_BYTES, copy_into, encode_copy_row
from data.brazos_layouts import (
    HEADER_SUFFIX,
    LAYOUTS,
    LAYOUTS_BY_MODEL,
    FileLayout,
    parse_header,
    parse_record,
)
from data.models import BrazosImportRun

PORTAL_URL = "https://brazoscad.org/certified-data-downloads/"
USER_AGENT = "TaxProtest-Django/1.0 (+https://github.com/PorkChopExpress86/TaxProtest-Django)"

DOWNLOAD_TIMEOUT = 60
DOWNLOAD_RETRIES = 4
DEFAULT_CHUNK_ROWS = 250_000
DEFAULT_ENCODING = "latin-1"

# Characters pulled from a source file per read, and lines sampled to infer the
# fixed record width.
READ_BLOCK_CHARS = 1 << 20
WIDTH_SAMPLE_LINES = 2000

_YEAR_RE = re.compile(r"(20\d{2})")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = "Download and bulk-load the Brazos CAD certified appraisal export."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year", type=int, help="Tax year to import (default: latest published)."
        )
        parser.add_argument("--list", action="store_true", help="List available years and exit.")
        parser.add_argument("--url", help="Explicit archive URL, bypassing the portal scrape.")
        parser.add_argument("--archive", help="Path to an already-downloaded .zip archive.")
        parser.add_argument(
            "--data-dir",
            help="Staging directory (default: $BRAZOS_DATA_DIR or <BASE_DIR>/data/cad_downloads).",
        )
        parser.add_argument(
            "--only",
            action="append",
            metavar="MODEL",
            help="Load only these models (repeatable). Default: all five.",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=DEFAULT_CHUNK_ROWS,
            help=f"Rows per COPY batch (default: {DEFAULT_CHUNK_ROWS}).",
        )
        parser.add_argument("--encoding", default=DEFAULT_ENCODING, help="Source file encoding.")
        parser.add_argument(
            "--record-width",
            type=int,
            help="Override the fixed record width instead of inferring it from the file.",
        )
        parser.add_argument(
            "--skip-download", action="store_true", help="Use existing files only; never fetch."
        )
        # Staged files are large (a year's extract is ~1.5 GB unpacked), so a
        # successful load reclaims them by default. Both escape hatches only
        # matter when you intend to re-run against the same files.
        parser.add_argument(
            "--keep-files",
            action="store_true",
            help="Keep the archive and extracted .TXT files after a successful load.",
        )
        parser.add_argument(
            "--keep-archive",
            action="store_true",
            help="Delete the extracted .TXT files but keep the .zip, so a re-run "
            "can skip the download.",
        )
        parser.add_argument(
            "--continue-on-error",
            action="store_true",
            help="Carry on with remaining files if one fails.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report row counts without writing to the database.",
        )

    # -- entry point --------------------------------------------------------

    def handle(self, *args, **options):
        self.verbosity = options["verbosity"]
        self.encoding = options["encoding"]
        self.dry_run = options["dry_run"]
        self.record_width = options["record_width"]

        if connection.vendor != "postgresql":
            raise CommandError(
                f"load_brazos_cad requires PostgreSQL (COPY); current backend is "
                f"'{connection.vendor}'."
            )

        layouts = self._selected_layouts(options["only"])
        data_dir = self._resolve_data_dir(options["data_dir"])

        if options["list"]:
            self._print_available()
            return

        archive_path, source_url, year = self._obtain_archive(options, data_dir)
        extract_dir = data_dir / "extracted" / f"{year}"
        members = self._extract(archive_path, extract_dir, layouts)
        header = self._read_header(archive_path)

        if header:
            self.stdout.write(
                f"Export: {header.get('file_description') or '?'} "
                f"(run {header.get('run_date') or '?'}, "
                f"year {header.get('appraisal_year') or '?'}, "
                f"supplement {header.get('supplement_num') or '?'}, "
                f"PACS {header.get('pacs_version') or '?'})"
            )
            declared = header.get("appraisal_year")
            if declared and int(declared) != year:
                self.stderr.write(
                    self.style.WARNING(
                        f"  ! archive header says year {declared}, loading as {year}"
                    )
                )

        run = None
        if not self.dry_run:
            run = BrazosImportRun.objects.create(
                tax_year=year,
                source_url=source_url or "",
                archive_name=archive_path.name,
                export_run_date=header.get("run_date") or "",
                export_description=header.get("file_description") or "",
                export_supplement_num=header.get("supplement_num"),
                export_dataset_id=header.get("dataset_id"),
                export_pacs_version=header.get("pacs_version") or "",
            )

        total_loaded = 0
        total_rejected = 0
        failures: list[str] = []

        try:
            for layout in layouts:
                path = members.get(layout.suffix)
                if path is None:
                    msg = f"{layout.suffix} not present in archive — skipped"
                    self.stderr.write(self.style.WARNING(f"  ! {msg}"))
                    failures.append(msg)
                    continue
                try:
                    loaded, rejected = self._load_file(layout, path, options["chunk_size"])
                    total_loaded += loaded
                    total_rejected += rejected
                except Exception as exc:  # noqa: BLE001 — reported, then re-raised or collected
                    msg = f"{layout.suffix}: {exc}"
                    failures.append(msg)
                    self.stderr.write(self.style.ERROR(f"  ✗ {msg}"))
                    if not options["continue_on_error"]:
                        raise
        except Exception as exc:
            if run is not None:
                run.status = "failed"
                run.finished_at = timezone.now()
                run.rows_loaded = total_loaded
                run.rows_rejected = total_rejected
                run.notes = str(exc)
                run.save()
            raise

        if run is not None:
            run.status = "completed" if not failures else "completed_with_errors"
            run.finished_at = timezone.now()
            run.rows_loaded = total_loaded
            run.rows_rejected = total_rejected
            run.notes = "\n".join(failures)
            run.save()

        # Only reclaim after a clean run: if a file failed, the staged copy is
        # what makes a retry cheap.
        if not self.dry_run and not failures and not options["keep_files"]:
            self._cleanup(
                archive_path,
                extract_dir,
                data_dir,
                keep_archive=options["keep_archive"],
                user_supplied_archive=bool(options["archive"]),
            )

        verb = "would load" if self.dry_run else "loaded"
        summary = f"Brazos CAD {year}: {verb} {total_loaded:,} rows"
        if total_rejected:
            summary += f", rejected {total_rejected:,}"
        if failures:
            self.stderr.write(self.style.WARNING(f"{summary} with {len(failures)} problem(s)."))
        else:
            self.stdout.write(self.style.SUCCESS(summary + "."))

    # -- setup helpers ------------------------------------------------------

    def _selected_layouts(self, only: list[str] | None) -> list[FileLayout]:
        if not only:
            return list(LAYOUTS)
        chosen = []
        for name in only:
            layout = LAYOUTS_BY_MODEL.get(name)
            if layout is None:
                raise CommandError(
                    f"Unknown model '{name}'. Choose from: {', '.join(LAYOUTS_BY_MODEL)}"
                )
            chosen.append(layout)
        # Preserve dependency order regardless of flag order.
        return [layout for layout in LAYOUTS if layout in chosen]

    def _resolve_data_dir(self, override: str | None) -> Path:
        raw = override or os.environ.get("BRAZOS_DATA_DIR")
        path = Path(raw) if raw else Path(settings.BASE_DIR) / "data" / "cad_downloads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- portal scraping ----------------------------------------------------

    def _fetch_portal(self) -> dict[int, str]:
        """Scrape the certified-downloads page into {year: archive_url}."""
        self._log(f"Scraping {PORTAL_URL}")
        try:
            response = requests.get(
                PORTAL_URL, timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Could not reach the BCAD portal: {exc}") from exc

        soup = BeautifulSoup(response.text, "html.parser")
        archives: dict[int, str] = {}

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href.lower().endswith(".zip"):
                continue
            url = requests.compat.urljoin(PORTAL_URL, href)
            # The anchor text ("Download 2025 Certified Data") states the data
            # year; the URL path only reflects when the file was uploaded, which
            # is not always the same year.
            match = _YEAR_RE.search(anchor.get_text(" ", strip=True)) or _YEAR_RE.search(
                Path(url).name
            )
            if not match:
                continue
            archives.setdefault(int(match.group(1)), url)

        if not archives:
            raise CommandError(
                "No .zip archives found on the BCAD portal — the page layout may have changed."
            )
        return archives

    def _print_available(self) -> None:
        archives = self._fetch_portal()
        self.stdout.write("Available Brazos CAD certified exports:")
        for year in sorted(archives, reverse=True):
            self.stdout.write(f"  {year}  {archives[year]}")

    # -- archive acquisition ------------------------------------------------

    def _obtain_archive(self, options: dict[str, Any], data_dir: Path) -> tuple[Path, str, int]:
        if options["archive"]:
            path = Path(options["archive"])
            if not path.exists():
                raise CommandError(f"Archive not found: {path}")
            year = options["year"] or self._infer_year(path.name)
            return path, "", year

        # With an explicit year and nothing to fetch, the target path is fully
        # determined — don't touch the network at all.
        if options["skip_download"] and options["year"] and not options["url"]:
            year = options["year"]
            target = data_dir / f"brazos_{year}_certified.zip"
            if not target.exists():
                raise CommandError(f"--skip-download given but {target} does not exist.")
            self._log(f"Using existing archive {target}")
            return target, "", year

        if options["url"]:
            url = options["url"]
            year = options["year"] or self._infer_year(Path(url).name)
        else:
            archives = self._fetch_portal()
            year = options["year"] or max(archives)
            if year not in archives:
                raise CommandError(
                    f"No archive published for {year}. Available: "
                    f"{', '.join(str(y) for y in sorted(archives, reverse=True))}"
                )
            url = archives[year]

        target = data_dir / f"brazos_{year}_certified.zip"
        if options["skip_download"]:
            if not target.exists():
                raise CommandError(f"--skip-download given but {target} does not exist.")
            self._log(f"Using existing archive {target}")
        else:
            self._download(url, target)
        return target, url, year

    def _infer_year(self, name: str) -> int:
        match = _YEAR_RE.search(name)
        if not match:
            raise CommandError(f"Could not infer a tax year from '{name}'; pass --year.")
        return int(match.group(1))

    def _download(self, url: str, target: Path) -> None:
        """Download with retry and HTTP range resume."""
        headers = {"User-Agent": USER_AGENT}
        expected = self._remote_size(url)

        if target.exists() and expected and target.stat().st_size == expected:
            self._log(f"Archive already complete ({expected:,} bytes) — skipping download.")
            return

        partial = target.with_suffix(".zip.part")
        last_error: Exception | None = None

        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            resume_from = partial.stat().st_size if partial.exists() else 0
            request_headers = dict(headers)
            mode = "wb"
            if resume_from and expected and resume_from < expected:
                request_headers["Range"] = f"bytes={resume_from}-"
                mode = "ab"
            elif resume_from:
                # No known total, or already at/over it — start clean.
                resume_from = 0

            try:
                self._log(
                    f"Downloading {url}"
                    + (f" (resuming at {resume_from:,} bytes)" if resume_from else "")
                    + f" [attempt {attempt}/{DOWNLOAD_RETRIES}]"
                )
                with requests.get(
                    url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers=request_headers
                ) as response:
                    response.raise_for_status()
                    if resume_from and response.status_code != 206:
                        # Server ignored the range request; restart from zero.
                        resume_from, mode = 0, "wb"
                    written = resume_from
                    started = time.monotonic()
                    with open(partial, mode) as fh:
                        for block in response.iter_content(CHUNK_BYTES):
                            if not block:
                                continue
                            fh.write(block)
                            written += len(block)
                            self._progress_bytes(written, expected, started)
                if self.verbosity >= 1:
                    self.stdout.write("")

                if expected and written != expected:
                    raise OSError(f"size mismatch: got {written:,}, expected {expected:,}")

                partial.replace(target)
                self._log(f"Saved {target} ({written:,} bytes)")
                return
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                backoff = 2**attempt
                self.stderr.write(
                    self.style.WARNING(f"  download failed ({exc}); retrying in {backoff}s")
                )
                time.sleep(backoff)

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

    def _progress_bytes(self, written: int, expected: int | None, started: float) -> None:
        if self.verbosity < 1:
            return
        elapsed = max(time.monotonic() - started, 1e-6)
        rate = written / elapsed / 1024 / 1024
        if expected:
            pct = written / expected * 100
            msg = f"\r    {written / 1e6:,.1f} / {expected / 1e6:,.1f} MB ({pct:5.1f}%) {rate:.1f} MB/s"
        else:
            msg = f"\r    {written / 1e6:,.1f} MB {rate:.1f} MB/s"
        self.stdout.write(msg, ending="")
        self.stdout.flush()

    # -- provenance ---------------------------------------------------------

    def _read_header(self, archive: Path) -> dict:
        """Read APPRAISAL_HEADER.TXT so the run records which export it loaded.

        Best-effort: a missing or unreadable header degrades to an empty dict
        rather than failing an otherwise good import.
        """
        try:
            with zipfile.ZipFile(archive) as zf:
                member = next(
                    (
                        i
                        for i in zf.infolist()
                        if Path(i.filename).name.upper().endswith(HEADER_SUFFIX)
                    ),
                    None,
                )
                if member is None:
                    return {}
                line = zf.read(member).decode(self.encoding, errors="replace")
        except (zipfile.BadZipFile, OSError) as exc:
            self.stderr.write(self.style.WARNING(f"  ! could not read export header: {exc}"))
            return {}

        header = parse_header(line.split("\r\n")[0].rstrip("\n"))
        # The dataset id is not in the header record itself, only in the member
        # filenames: 2025-07-23_002022_APPRAISAL_HEADER.TXT -> 2022.
        if match := re.search(r"_(\d+)_APPRAISAL", Path(member.filename).name, re.I):
            header["dataset_id"] = int(match.group(1))
        return header

    # -- extraction ---------------------------------------------------------

    def _extract(
        self, archive: Path, extract_dir: Path, layouts: list[FileLayout]
    ) -> dict[str, Path]:
        """Extract the members we need. Returns {layout suffix: extracted path}."""
        extract_dir.mkdir(parents=True, exist_ok=True)
        wanted = {layout.suffix for layout in layouts}
        found: dict[str, Path] = {}

        try:
            zf = zipfile.ZipFile(archive)
        except zipfile.BadZipFile as exc:
            raise CommandError(
                f"{archive} is not a readable zip ({exc}). Delete it and re-run to re-download."
            ) from exc

        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Real members are prefixed with the export date and dataset id,
                # e.g. 2025-07-23_002022_APPRAISAL_INFO.TXT.
                member_name = Path(info.filename).name
                match = next((s for s in wanted if member_name.upper().endswith(s)), None)
                if match is None:
                    continue

                destination = extract_dir / match
                if destination.exists() and destination.stat().st_size == info.file_size:
                    self._log(f"  {match} already extracted ({info.file_size:,} bytes)")
                    found[match] = destination
                    continue

                self._log(f"  extracting {match} ({info.file_size:,} bytes)")
                with zf.open(info) as src, open(destination, "wb") as dst:
                    while block := src.read(CHUNK_BYTES):
                        dst.write(block)
                found[match] = destination

        missing = wanted - set(found)
        if missing:
            self.stderr.write(
                self.style.WARNING(f"  ! not in archive: {', '.join(sorted(missing))}")
            )
        return found

    # -- loading ------------------------------------------------------------

    def _detect_record_width(self, path: Path, layout: FileLayout) -> int:
        """Infer the true record width from the file.

        Taken as the most common line length over a leading sample rather than
        the length of the first line, so a record containing an embedded newline
        cannot skew the result. The published layout is not authoritative here:
        the 2025 property export emits 9247-character records against a
        documented 9067, because districts add trailing fields over time.
        """
        counts: Counter[int] = Counter()
        with open(path, encoding=self.encoding, newline="") as fh:
            for index, raw in enumerate(fh):
                if index >= WIDTH_SAMPLE_LINES:
                    break
                body = raw.rstrip("\r\n")
                if body.strip():
                    counts[len(body)] += 1

        if not counts:
            raise CommandError(f"{path.name} contains no usable records.")

        width, _ = counts.most_common(1)[0]
        if width < layout.min_width:
            raise CommandError(
                f"{path.name}: detected record width {width} is shorter than the "
                f"{layout.min_width} characters {layout.model} maps. The export format "
                f"has changed — check data/brazos_layouts.py, or pass --record-width."
            )
        return width

    def _records(self, path: Path, width: int) -> Iterator[str]:
        """Yield exact fixed-width records.

        Deliberately *not* line-based. PACS exports occasionally contain a raw
        CR or LF inside a text field — a 2024 owner name did this — which splits
        one logical record across several physical lines. Reading line by line
        then silently loads a truncated record whose value fields all land on
        blanks. Slicing a character stream at the record width instead treats an
        embedded newline as ordinary field content, which is what it is.
        """
        carry = ""
        with open(path, encoding=self.encoding, newline="") as fh:
            while True:
                block = fh.read(READ_BLOCK_CHARS)
                at_eof = not block
                buf = carry + block
                pos = 0
                # Off EOF, keep two characters in hand so a CRLF terminator is
                # never split across block boundaries.
                need = width if at_eof else width + 2
                while len(buf) - pos >= need:
                    record = buf[pos : pos + width]
                    pos += width
                    if buf[pos : pos + 2] == "\r\n":
                        pos += 2
                    elif buf[pos : pos + 1] in ("\r", "\n"):
                        pos += 1
                    yield record
                carry = buf[pos:]
                if at_eof:
                    break

        if carry.strip():
            # Truncated trailing record; the natural-key check decides its fate.
            yield carry

    def _rows(
        self, layout: FileLayout, path: Path, width: int
    ) -> Iterator[tuple[list[str | None], bool]]:
        """Yield (values, accepted) per record, rejecting those missing a natural key."""
        key_positions = [layout.column_names.index(c) for c in layout.conflict_columns]
        min_width = layout.min_width

        for record in self._records(path, width):
            if not record.strip():
                continue
            if len(record) < min_width:
                record = record.ljust(min_width)
            values = parse_record(layout, record)
            accepted = all(values[i] is not None for i in key_positions)
            yield values, accepted

    def _load_file(self, layout: FileLayout, path: Path, chunk_rows: int) -> tuple[int, int]:
        db_table = apps.get_model("data", layout.model)._meta.db_table
        self.stdout.write(
            f"Loading {layout.label} from {path.name} "
            f"({path.stat().st_size / 1e6:,.1f} MB) -> {db_table}"
        )

        width = self.record_width or self._detect_record_width(path, layout)
        self._log(f"    record width: {width} characters")

        loaded = rejected = 0
        started = time.monotonic()
        source = self._rows(layout, path, width)

        if self.dry_run:
            for _values, accepted in source:
                loaded += accepted
                rejected += not accepted
            self._log(f"  dry run: {loaded:,} parseable, {rejected:,} rejected")
            return loaded, rejected

        staging = f"stg_{db_table}"
        self._create_staging(staging, layout)
        try:
            while True:
                batch_loaded, batch_rejected, exhausted = self._load_chunk(
                    layout, db_table, staging, source, chunk_rows
                )
                loaded += batch_loaded
                rejected += batch_rejected
                if batch_loaded:
                    elapsed = max(time.monotonic() - started, 1e-6)
                    self._log(
                        f"    {loaded:,} rows ({loaded / elapsed:,.0f}/s)"
                        + (f", {rejected:,} rejected" if rejected else "")
                    )
                if exhausted:
                    break
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP TABLE IF EXISTS "{staging}"')

        elapsed = max(time.monotonic() - started, 1e-6)
        self.stdout.write(
            self.style.SUCCESS(
                f"  ✓ {layout.label}: {loaded:,} rows in {elapsed:,.1f}s "
                f"({loaded / elapsed:,.0f}/s)" + (f", {rejected:,} rejected" if rejected else "")
            )
        )
        return loaded, rejected

    def _create_staging(self, staging: str, layout: FileLayout) -> None:
        columns = ", ".join(f'"{f.name}" {f.staging_type}' for f in layout.fields)
        with connection.cursor() as cursor:
            cursor.execute(f'DROP TABLE IF EXISTS "{staging}"')
            # UNLOGGED skips WAL for the staging copy: it is rebuilt from the
            # source file on any crash, so durability buys nothing here.
            # _rn preserves file order so the last duplicate wins, consistently
            # both within a chunk and across chunks.
            cursor.execute(f'CREATE UNLOGGED TABLE "{staging}" ({columns}, _rn bigserial)')

    def _load_chunk(
        self,
        layout: FileLayout,
        db_table: str,
        staging: str,
        source: Iterator[tuple[list[str | None], bool]],
        chunk_rows: int,
    ) -> tuple[int, int, bool]:
        """COPY up to chunk_rows records into staging, then merge. Returns
        (loaded, rejected, source_exhausted)."""
        loaded = 0
        rejected = 0
        exhausted = False

        def generate() -> Iterator[str]:
            nonlocal loaded, rejected, exhausted
            for _ in range(chunk_rows):
                try:
                    values, accepted = next(source)
                except StopIteration:
                    exhausted = True
                    return
                if not accepted:
                    rejected += 1
                    continue
                loaded += 1
                yield encode_copy_row(values)

        columns = ", ".join(f'"{name}"' for name in layout.column_names)
        copy_sql = f'COPY "{staging}" ({columns}) FROM STDIN WITH (FORMAT text)'

        with transaction.atomic():
            with connection.cursor() as cursor:
                copy_into(cursor, copy_sql, generate())
                if loaded:
                    cursor.execute(self._merge_sql(layout, db_table, staging))
                cursor.execute(f'TRUNCATE "{staging}"')

        return loaded, rejected, exhausted

    def _merge_sql(self, layout: FileLayout, db_table: str, staging: str) -> str:
        """INSERT ... SELECT DISTINCT ON ... ON CONFLICT DO UPDATE."""
        names = layout.column_names
        conflict = layout.conflict_columns
        quoted = ", ".join(f'"{n}"' for n in names)
        conflict_cols = ", ".join(f'"{n}"' for n in conflict)
        order_by = ", ".join(f'"{n}"' for n in conflict)

        updatable = [n for n in names if n not in conflict]
        assignments = ", ".join(f'"{n}" = EXCLUDED."{n}"' for n in updatable)
        assignments = f"{assignments}, " if assignments else ""

        return (
            f'INSERT INTO "{db_table}" ({quoted}, "created_at", "updated_at") '
            f"SELECT DISTINCT ON ({order_by}) {quoted}, now(), now() "
            f'FROM "{staging}" ORDER BY {order_by}, _rn DESC '
            f"ON CONFLICT ({conflict_cols}) DO UPDATE SET "
            f'{assignments}"updated_at" = now()'
        )

    # -- misc ---------------------------------------------------------------

    def _cleanup(
        self,
        archive: Path,
        extract_dir: Path,
        data_dir: Path,
        *,
        keep_archive: bool,
        user_supplied_archive: bool,
    ) -> None:
        """Reclaim the staging area now that the rows are in PostgreSQL."""
        freed = 0

        if extract_dir.exists():
            for child in sorted(extract_dir.iterdir()):
                if child.is_file():
                    freed += child.stat().st_size
                    child.unlink(missing_ok=True)
            with suppress(OSError):  # non-empty only if something else wrote here
                extract_dir.rmdir()
            with suppress(OSError):
                extract_dir.parent.rmdir()

        # Never delete an archive the caller pointed us at with --archive, and
        # never reach outside the staging directory.
        may_delete_archive = (
            not keep_archive
            and not user_supplied_archive
            and archive.exists()
            and data_dir.resolve() in archive.resolve().parents
        )
        if may_delete_archive:
            freed += archive.stat().st_size
            archive.unlink(missing_ok=True)
        elif archive.exists() and not keep_archive and user_supplied_archive:
            self._log(f"  keeping caller-supplied archive {archive}")

        if freed:
            size = f"{freed / 1e9:,.2f} GB" if freed >= 1e9 else f"{freed / 1e6:,.1f} MB"
            self._log(f"Cleaned up staged files, freed {size}.")

    def _log(self, message: str) -> None:
        if self.verbosity >= 1:
            self.stdout.write(message)
