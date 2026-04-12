import io
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.test import TestCase, override_settings

from data.models import DownloadRecord
from data.tasks_new import download_and_extract_hcad


def make_zip_bytes(filename: str = 'payload.txt', content: str = 'ok') -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, *, url: str, status_code: int = 200, content: bytes = b''):
        self.url = url
        self.status_code = status_code
        self.raw = io.BytesIO(content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.url = self.url
            raise requests.HTTPError(f'{self.status_code} for {self.url}', response=response)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.raw.close()
        return False


class DownloadAndExtractHCADTests(TestCase):
    def setUp(self):
        super().setUp()
        self.zip_bytes = make_zip_bytes()
        self.mock_datetime = Mock()
        self.mock_datetime.now.return_value = datetime(2026, 4, 12)

    def fake_get_with_optional_missing(self, url, stream=True, timeout=300):
        if 'Real_acct_ownership_history.zip' in url:
            return FakeResponse(url=url, status_code=404)
        if '/CAMA/2026/' in url:
            return FakeResponse(url=url, status_code=404)
        return FakeResponse(url=url, content=self.zip_bytes)

    def fake_get_with_required_missing(self, url, stream=True, timeout=300):
        if 'Real_acct_owner.zip' in url:
            return FakeResponse(url=url, status_code=404)
        return FakeResponse(url=url, content=self.zip_bytes)

    def test_download_and_extract_hcad_skips_missing_optional_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(BASE_DIR=tmpdir):
            with patch('data.tasks_new.datetime', self.mock_datetime), patch(
                'data.tasks_new.requests.get',
                side_effect=self.fake_get_with_optional_missing,
            ):
                results = download_and_extract_hcad.run()

            results_by_name = {result['filename']: result for result in results}

            self.assertIn('Real_acct_ownership_history.zip', results_by_name)
            self.assertTrue(results_by_name['Real_acct_ownership_history.zip']['skipped'])
            self.assertTrue(results_by_name['Real_acct_ownership_history.zip']['optional'])
            self.assertIn('/CAMA/2025/', results_by_name['Real_acct_owner.zip']['url'])
            self.assertTrue(results_by_name['Real_acct_owner.zip']['extracted'])
            self.assertFalse(
                DownloadRecord.objects.filter(filename='Real_acct_ownership_history.zip').exists()
            )
            self.assertTrue(
                Path(tmpdir, 'downloads', 'Real_acct_owner', 'payload.txt').exists()
            )

    def test_download_and_extract_hcad_raises_for_missing_required_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(BASE_DIR=tmpdir):
            with patch('data.tasks_new.datetime', self.mock_datetime), patch(
                'data.tasks_new.requests.get',
                side_effect=self.fake_get_with_required_missing,
            ):
                with self.assertRaises(requests.HTTPError):
                    download_and_extract_hcad.run()

        self.assertFalse(DownloadRecord.objects.filter(filename='Real_acct_owner.zip').exists())