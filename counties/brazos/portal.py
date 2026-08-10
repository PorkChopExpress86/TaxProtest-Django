"""Shared BCAD portal download/extract/resolve helpers.

load_brazos_cad, load_brazos_gis, and import_brazos_assessment_history each
stream-download one certified-export archive and extract it as a zip. This
module is the one place that does both, so a fix to the download or extract
logic (a timeout adjustment, a new failure mode) lands once instead of being
re-applied to three near-identical copies.

Each command still owns its own portal-scraping logic (which URL to hit,
which links on that page are real archives) -- that varies enough per portal
(see load_brazos_gis's CERTIFIED_LINK_RE filter, import_brazos_assessment_history's
multi-year "return every year found" vs. load_brazos_cad's single-latest) that
unifying it would trade three small, real differences for one function with
enough flags to reimplement them anyway. Only download/extract, which really
were byte-for-byte identical, are shared here.

``resolve_timestamped_file`` is also shared: locating the real, timestamp-
prefixed file for a known target filename after extraction is the same
lookup in ``load_brazos_cad`` and ``import_brazos_assessment_history``, with
the same "ISO prefixes sort correctly as strings" rationale behind it (see
its docstring). ``load_brazos_gis``'s ``_find_shapefile`` is a different,
simpler idiom -- exactly one shapefile is expected, so there's no "newest of
several timestamped copies" question to answer -- and stays where it is.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Callable
from pathlib import Path

import requests
from django.core.management.base import CommandError

logger = logging.getLogger("brazos_cad")

USER_AGENT = "TaxProtest-Django/1.0 (+brazos_cad loader)"
DOWNLOAD_TIMEOUT = 300  # seconds


def download_archive(
    url: str,
    destination: Path,
    *,
    force: bool,
    dry_run: bool,
    log: Callable[[str], None],
) -> Path:
    """Stream-download ``url`` to ``destination``, skipping if already on disk."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        log(f"Archive already on disk: {destination}")
        return destination

    if dry_run:
        log(f"[dry-run] would download {url} -> {destination}")
        return destination

    log(f"Downloading {url} -> {destination}")
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


def extract_zip(
    archive: Path,
    extract_dir: Path,
    *,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """Extract a zip archive into ``extract_dir``.

    Raises ``CommandError`` for a missing or corrupt archive. Raises the bare
    ``zipfile`` ``NotImplementedError`` for a compression method it can't
    handle (e.g. DEFLATE64) -- callers old enough to hit that (today: just
    ``import_brazos_assessment_history``) catch it themselves and fall back
    to the system ``7z`` binary; forcing that fallback onto every caller would
    add an untriggered branch to the two that never reach years old enough to
    need it.
    """
    if not archive.exists():
        raise CommandError(f"Archive not found: {archive}")
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            if dry_run:
                log(f"[dry-run] would extract {len(names)} files into {extract_dir}")
                return
            zf.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise CommandError(f"Archive is not a valid zip: {archive} ({exc})") from exc

    log(f"Extracted into {extract_dir}")


def resolve_timestamped_file(
    extract_dir: Path, filename: str, *, warn_on_missing: bool = True
) -> Path | None:
    """Find the newest on-disk copy of a timestamp-prefixed BCAD export file.

    Real BCAD export filenames carry a "YYYY-MM-DD_HHMMSS_" timestamp prefix
    (e.g. "2025-07-23_002022_APPRAISAL_INFO.TXT") and may sit in a nested
    directory (confirmed: 2025 extracts flat, 2022 nests everything under
    "2022 CERTIFICATION EXPORT/"), so ``filename`` is matched by suffix via a
    recursive search rather than an exact name/path. If extraction (e.g. a
    ``--force`` re-run) left more than one timestamped copy of the same
    target file on disk, ISO-formatted prefixes sort correctly as strings,
    so picking the lexicographically-last match picks the newest.

    Set ``warn_on_missing=False`` when the caller already turns a ``None``
    result into its own error or warning, to avoid logging the same missing
    file twice.
    """
    matches = sorted(extract_dir.rglob(f"*{filename}"), reverse=True)
    if not matches:
        if warn_on_missing:
            logger.warning("Missing expected file: %s", filename)
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple candidates for %s under %s; using the newest: %s",
            filename,
            extract_dir,
            matches[0].name,
        )
    return matches[0]
