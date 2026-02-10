#!/usr/bin/env python3
import os
import requests
import zipfile
import shutil
import sys
from datetime import datetime

# Define URLs (mirrored from data/tasks.py)
# Note: Using hardcoded year or logic? tasks.py used 2025.
# We will use the same list.
HCAD_URLS = [
    'https://download.hcad.org/data/CAMA/2025/Real_acct_owner.zip',
    'https://download.hcad.org/data/CAMA/2025/Real_acct_ownership_history.zip',
    'https://download.hcad.org/data/CAMA/2025/Real_building_land.zip',
    'https://download.hcad.org/data/CAMA/2025/Code_description_real.zip',
    'https://download.hcad.org/data/CAMA/2025/PP_files.zip',
    'https://download.hcad.org/data/CAMA/2025/Code_description_pp.zip',
    'https://download.hcad.org/data/CAMA/2025/Hearing_files.zip',
    'https://download.hcad.org/data/GIS/Parcels.zip',
]

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')

def ensure_download_dir():
    if not os.path.exists(DOWNLOAD_DIR):
        print(f"Creating download directory: {DOWNLOAD_DIR}")
        os.makedirs(DOWNLOAD_DIR)

def download_file(url):
    local_name = url.split('/')[-1]
    local_path = os.path.join(DOWNLOAD_DIR, local_name)
    
    print(f"Downloading {url}...")
    try:
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        print(f"Saved to {local_path}")
        return local_path
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        return None

def extract_file(local_path):
    if not local_path or not os.path.exists(local_path):
        return
    
    print(f"Extracting {local_path}...")
    try:
        local_name = os.path.basename(local_path)
        if zipfile.is_zipfile(local_path):
            with zipfile.ZipFile(local_path, 'r') as z:
                # Logic from data/tasks.py: extract to folder or flat?
                # tasks.py: extract_to = os.path.join(download_dir, local_name.replace('.zip', ''))
                # But wait, import_building_data expects 'extract_dir/building_res.txt'.
                # Let's verify where tasks.py extracts.
                # data/tasks.py:55: extract_to = os.path.join(download_dir, local_name.replace('.zip', ''))
                
                folder_name = local_name.replace('.zip', '')
                extract_to = os.path.join(DOWNLOAD_DIR, folder_name)
                os.makedirs(extract_to, exist_ok=True)
                z.extractall(extract_to)
                print(f"Extracted to {extract_to}")
    except Exception as e:
        print(f"Error extracting {local_path}: {e}")

def main():
    print("Starting build-time data download...")
    ensure_download_dir()
    
    success = True
    for url in HCAD_URLS:
        # Check if year needs to be dynamic? data/tasks.py has year hardcoded in list but dynamic in one function.
        # The list at top of tasks.py has 2025. We stick to that.
        fpath = download_file(url)
        if fpath:
            extract_file(fpath)
        else:
            success = False
            
    if success:
        print("All downloads completed successfully.")
    else:
        print("Some downloads failed.")
        sys.exit(1)

if __name__ == '__main__':
    main()
