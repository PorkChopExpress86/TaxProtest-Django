"""Shared BCAD portal download/extract helpers.

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
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from pathlib import Path

import requests
from django.core.management.base import CommandError

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
