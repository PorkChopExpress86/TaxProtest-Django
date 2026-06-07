#!/usr/bin/env python3
"""
Build-time script to download HCAD data archives.

Run during `docker build` so the compressed archives are baked into the image
(copied to /hcad_downloads_baked) and ready when the container starts. The
container entrypoint syncs those archives into the runtime volume and the ETL
pipeline extracts + imports them at startup.

This script only DOWNLOADS — it deliberately does not extract. The build-time
extract directory lives under /app/var, which the runtime mounts a volume over,
so anything extracted here would be discarded. Extraction happens at runtime
instead (see scripts/entrypoint.sh).

Tries the current year first, then falls back to the prior year — matching the
logic used by the Celery tasks and the modern ETL DownloadManager at runtime.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Ensure repository root is importable when script is executed as
# `python scripts/build_time_download.py` from /app.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from taxprotest.runtime_paths import resolve_runtime_paths

RUNTIME_PATHS = resolve_runtime_paths(BASE_DIR)
DOWNLOAD_DIR = os.fspath(RUNTIME_PATHS.download_dir)

CHUNK_SIZE = 1 << 20  # 1 MiB — fewer syscalls than the default 8 KiB on multi-GB files
DOWNLOAD_TIMEOUT = 600


def candidate_years() -> list[int]:
    year = datetime.now().year
    return [year, year - 1]


def build_candidate_urls(filename: str, gis_url: str | None = None) -> list[str]:
    if gis_url:
        return [gis_url]
    return [f"https://download.hcad.org/data/CAMA/{year}/{filename}" for year in candidate_years()]


# Required archives — build fails if any of these are missing.
# Optional archives — skipped silently on 404.
ARCHIVES = [
    {"filename": "Real_acct_owner.zip", "required": True},
    {"filename": "Real_acct_ownership_history.zip", "required": False},
    {"filename": "Real_building_land.zip", "required": True},
    {"filename": "Real_jur_exempt.zip", "required": False},
    {"filename": "Code_description_real.zip", "required": False},
    {"filename": "PP_files.zip", "required": False},
    {"filename": "Code_description_pp.zip", "required": False},
    {"filename": "Hearing_files.zip", "required": False},
    {
        "filename": "Parcels.zip",
        "required": True,
        "gis_url": "https://download.hcad.org/data/GIS/Parcels.zip",
    },
]


def make_session() -> requests.Session:
    """Session with connection pooling and retry/backoff on transient failures."""
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,  # 0s, 1s, 2s, 4s, 8s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def ensure_download_dir() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def download_with_fallback(session: requests.Session, archive: dict) -> str | None:
    """Try each candidate URL in turn; return local path on success, None if optional and skipped."""
    filename = archive["filename"]
    local_path = os.path.join(DOWNLOAD_DIR, filename)
    candidate_urls = build_candidate_urls(filename, archive.get("gis_url"))

    for url in candidate_urls:
        print(f"  Trying {url} ...", flush=True)
        try:
            with session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
                r.raise_for_status()
                expected = int(r.headers.get("content-length", 0))
                written = 0
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)

            # Validate completeness so a truncated transfer is never baked into
            # the image as a "successful" download.
            if expected and written != expected:
                raise OSError(
                    f"Size mismatch for {filename}: got {written:,} bytes, expected {expected:,}"
                )

            size_mb = written / 1_048_576
            print(f"  Saved {filename} ({size_mb:.1f} MB)", flush=True)
            return local_path
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                print(f"  404 at {url} — trying next candidate", flush=True)
            else:
                print(f"  HTTP error at {url}: {exc}", flush=True)
            if os.path.exists(local_path):
                os.remove(local_path)
        except (requests.RequestException, OSError) as exc:
            print(f"  Download failed for {url}: {exc}", flush=True)
            if os.path.exists(local_path):
                os.remove(local_path)

    if archive.get("required", True):
        print(f"ERROR: Required archive {filename} could not be downloaded.", flush=True)
        sys.exit(1)

    print(f"  Skipping optional archive {filename} (not available)", flush=True)
    return None


def main() -> None:
    print("=" * 60, flush=True)
    print(f"Build-time HCAD download  (candidates: {candidate_years()})", flush=True)
    print("=" * 60, flush=True)
    ensure_download_dir()
    session = make_session()

    # Download all archives in parallel (each writes to its own file).
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(download_with_fallback, session, archive): archive for archive in ARCHIVES
        }
        for future in as_completed(futures):
            future.result()  # re-raises SystemExit from required-archive failures

    print(
        "\nBuild-time download complete (archives only; extraction happens at runtime).", flush=True
    )


if __name__ == "__main__":
    main()
