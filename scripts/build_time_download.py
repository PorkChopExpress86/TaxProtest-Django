#!/usr/bin/env python3
"""
Build-time script to download and extract HCAD data files.

Run during `docker build` so data is ready when the container starts.
Tries the current year first, then falls back to the prior year — matching
the same logic used by the Celery tasks at runtime.
"""

import os
import shutil
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Ensure repository root is importable when script is executed as
# `python scripts/build_time_download.py` from /app.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from taxprotest.runtime_paths import resolve_runtime_paths

RUNTIME_PATHS = resolve_runtime_paths(BASE_DIR)
DOWNLOAD_DIR = os.fspath(RUNTIME_PATHS.download_dir)
EXTRACT_DIR = os.fspath(RUNTIME_PATHS.extract_dir)


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


def ensure_download_dir() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(EXTRACT_DIR, exist_ok=True)


def download_with_fallback(archive: dict) -> str | None:
    """Try each candidate URL in turn; return local path on success, None if optional and skipped."""
    filename = archive["filename"]
    local_path = os.path.join(DOWNLOAD_DIR, filename)
    candidate_urls = build_candidate_urls(filename, archive.get("gis_url"))

    for url in candidate_urls:
        print(f"  Trying {url} ...", flush=True)
        try:
            with requests.get(url, stream=True, timeout=600) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    shutil.copyfileobj(r.raw, f)
            size_mb = os.path.getsize(local_path) / 1_048_576
            print(f"  Saved {filename} ({size_mb:.1f} MB)", flush=True)
            return local_path
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                print(f"  404 at {url} — trying next candidate", flush=True)
            else:
                print(f"  HTTP error at {url}: {exc}", flush=True)
            if os.path.exists(local_path):
                os.remove(local_path)
        except requests.RequestException as exc:
            print(f"  Request failed for {url}: {exc}", flush=True)
            if os.path.exists(local_path):
                os.remove(local_path)

    if archive.get("required", True):
        print(f"ERROR: Required archive {filename} could not be downloaded.", flush=True)
        sys.exit(1)

    print(f"  Skipping optional archive {filename} (not available)", flush=True)
    return None


def extract(local_path: str) -> None:
    if not zipfile.is_zipfile(local_path):
        return
    folder_name = os.path.basename(local_path).replace(".zip", "")
    extract_to = os.path.join(EXTRACT_DIR, folder_name)
    os.makedirs(extract_to, exist_ok=True)
    print(f"  Extracting to {extract_to} ...", flush=True)
    with zipfile.ZipFile(local_path, "r") as z:
        z.extractall(extract_to)


def main() -> None:
    print("=" * 60, flush=True)
    print(f"Build-time HCAD download  (candidates: {candidate_years()})", flush=True)
    print("=" * 60, flush=True)
    ensure_download_dir()

    # Download all archives in parallel (each writes to its own file).
    # Extraction runs after all downloads complete to keep output readable.
    downloaded: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(download_with_fallback, archive): archive for archive in ARCHIVES}
        for future in as_completed(futures):
            archive = futures[future]
            local_path = future.result()  # sys.exit(1) inside on required failures
            if local_path:
                downloaded.append(local_path)

    for local_path in downloaded:
        extract(local_path)

    print("\nBuild-time download complete.", flush=True)


if __name__ == "__main__":
    main()
