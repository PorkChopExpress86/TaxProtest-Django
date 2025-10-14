import os
import zipfile
import shutil
import requests
from celery import shared_task
from django.conf import settings
from .models import DownloadRecord


HCAD_URLS = [
    'https://download.hcad.org/data/CAMA/2025/Real_acct_owner.zip',
    'https://download.hcad.org/data/CAMA/2025/Real_acct_ownership_history.zip',
    'https://download.hcad.org/data/CAMA/2025/Code_description_real.zip',
    'https://download.hcad.org/data/CAMA/2025/PP_files.zip',
    'https://download.hcad.org/data/CAMA/2025/Code_description_pp.zip',
    'https://download.hcad.org/data/CAMA/2025/Hearing_files.zip',
]


def ensure_download_dir():
    download_dir = os.path.join(settings.BASE_DIR, 'downloads')
    os.makedirs(download_dir, exist_ok=True)
    return download_dir


@shared_task(bind=True)
def download_and_extract_hcad(self):
    """Download a set of HCAD ZIP files, save them to downloads/, and extract them.

    Records a DownloadRecord per file.
    """
    download_dir = ensure_download_dir()
    results = []
    for url in HCAD_URLS:
        local_name = url.split('/')[-1]
        local_path = os.path.join(download_dir, local_name)

        # stream download
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)

        # create DB record
        rec = DownloadRecord.objects.create(url=url, filename=local_name)

        # try to extract if zip
        try:
            if zipfile.is_zipfile(local_path):
                with zipfile.ZipFile(local_path, 'r') as z:
                    extract_to = os.path.join(download_dir, local_name.replace('.zip', ''))
                    os.makedirs(extract_to, exist_ok=True)
                    z.extractall(extract_to)
                rec.extracted = True
                rec.save()
        except Exception as ex:
            # Do not fail the whole task on one file; log and continue
            self.retry(exc=ex, countdown=30, max_retries=2)

        results.append({'url': url, 'local': local_path, 'extracted': rec.extracted})

    return results
import csv
import io
import requests
from celery import shared_task

from django.conf import settings

from .models import PropertyRecord


@shared_task(bind=True)
def download_extract_reload(self, source_url):
    """
    Downloads a CSV from `source_url`, parses rows, and reloads into the database.

    Expected CSV columns: address, city, zipcode, value
    """
    resp = requests.get(source_url, timeout=30)
    resp.raise_for_status()

    text = resp.content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))

    # For simplicity: remove previous records that match this source_url
    PropertyRecord.objects.filter(source_url=source_url).delete()

    objs = []
    for row in reader:
        try:
            value = row.get("value") or row.get("assessed_value") or None
            if value:
                value = float(value.replace(',', ''))
        except Exception:
            value = None

        objs.append(
            PropertyRecord(
                address=(row.get("address") or "").strip(),
                city=(row.get("city") or "").strip(),
                zipcode=(row.get("zipcode") or "").strip(),
                value=value,
                source_url=source_url,
            )
        )

    PropertyRecord.objects.bulk_create(objs)
    return {"loaded": len(objs)}
