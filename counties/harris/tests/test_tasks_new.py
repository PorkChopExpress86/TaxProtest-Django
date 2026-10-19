import io
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import urllib3
from django.test import SimpleTestCase, TestCase, override_settings

from counties.common.models import ImportOperation
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisFailurePolicy,
    HarrisImportStatus,
    HarrisPreview,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.models import DownloadRecord
from counties.harris.tasks_new import (
    HCAD_ARCHIVE_SOURCES,
    _run_authoritative_pipeline,
    download_and_extract_hcad,
    download_and_import_building_data,
    download_and_import_gis_data,
    download_archive_with_fallback,
    run_etl_pipeline,
)


def make_zip_bytes(filename: str = "payload.txt", content: str = "ok") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, *, url: str, status_code: int = 200, content: bytes = b""):
        self.url = url
        self.status_code = status_code
        self.raw = io.BytesIO(content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.url = self.url
            raise requests.HTTPError(f"{self.status_code} for {self.url}", response=response)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.raw.close()
        return False


class BrokenRaw:
    def __init__(self, first_chunk: bytes):
        self.chunks = [first_chunk]

    def read(self, size: int = -1) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        raise urllib3.exceptions.ProtocolError("connection broken")

    def close(self) -> None:
        pass


FULL_ARCHIVE = b"complete archive bytes"


class DownloadArchiveWithFallbackTests(SimpleTestCase):
    def assert_failed_download_leaves_nothing_and_next_run_completes(
        self, failing_get, expected_error: type[Exception]
    ):
        source = HCAD_ARCHIVE_SOURCES[0]

        def complete_get(url, stream=True, timeout=300):
            return FakeResponse(url=url, content=FULL_ARCHIVE)

        with tempfile.TemporaryDirectory() as download_dir:
            with patch("counties.harris.tasks_new.requests.get", side_effect=failing_get):
                with self.assertRaises(expected_error):
                    download_archive_with_fallback(source, download_dir, reference_year=2026)
            self.assertEqual(sorted(Path(download_dir).iterdir()), [])

            with patch("counties.harris.tasks_new.requests.get", side_effect=complete_get):
                url, local_path = download_archive_with_fallback(
                    source, download_dir, reference_year=2026
                )

            self.assertIn("/2026/", url)
            self.assertEqual(Path(local_path).read_bytes(), FULL_ARCHIVE)
            self.assertEqual(sorted(Path(download_dir).iterdir()), [Path(local_path)])

    def test_interrupted_stream_leaves_nothing_and_next_run_downloads_again(self):
        def broken_get(url, stream=True, timeout=300):
            response = FakeResponse(url=url)
            response.raw = BrokenRaw(FULL_ARCHIVE[:9])
            return response

        self.assert_failed_download_leaves_nothing_and_next_run_completes(
            broken_get, urllib3.exceptions.ProtocolError
        )


class DownloadAndExtractHCADTests(TestCase):
    def setUp(self):
        super().setUp()
        self.zip_bytes = make_zip_bytes()
        self.mock_datetime = Mock()
        self.mock_datetime.now.return_value = datetime(2026, 4, 12)

    def fake_get_with_optional_missing(self, url, stream=True, timeout=300):
        if "Real_acct_ownership_history.zip" in url:
            return FakeResponse(url=url, status_code=404)
        if "/CAMA/2026/" in url:
            return FakeResponse(url=url, status_code=404)
        return FakeResponse(url=url, content=self.zip_bytes)

    def fake_get_with_required_missing(self, url, stream=True, timeout=300):
        if "Real_acct_owner.zip" in url:
            return FakeResponse(url=url, status_code=404)
        return FakeResponse(url=url, content=self.zip_bytes)

    @staticmethod
    def runtime_overrides(tmpdir: str) -> dict[str, str]:
        return {
            "BASE_DIR": tmpdir,
            "HCAD_DOWNLOAD_DIR": str(Path(tmpdir) / "var" / "downloads"),
            "HCAD_EXTRACT_DIR": str(Path(tmpdir) / "var" / "extracted"),
            "HCAD_LOG_DIR": str(Path(tmpdir) / "var" / "logs"),
            "PROJECT_REPORT_DIR": str(Path(tmpdir) / "var" / "reports"),
        }

    def test_download_and_extract_hcad_skips_missing_optional_archive(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            override_settings(**self.runtime_overrides(tmpdir)),
        ):
            with (
                patch("counties.harris.tasks_new.datetime", self.mock_datetime),
                patch(
                    "counties.harris.tasks_new.requests.get",
                    side_effect=self.fake_get_with_optional_missing,
                ),
            ):
                results = download_and_extract_hcad.run()

            results_by_name = {result["filename"]: result for result in results}

            self.assertIn("Real_acct_ownership_history.zip", results_by_name)
            self.assertTrue(results_by_name["Real_acct_ownership_history.zip"]["skipped"])
            self.assertTrue(results_by_name["Real_acct_ownership_history.zip"]["optional"])
            self.assertIn("/CAMA/2025/", results_by_name["Real_acct_owner.zip"]["url"])
            self.assertTrue(results_by_name["Real_acct_owner.zip"]["extracted"])
            self.assertFalse(
                DownloadRecord.objects.filter(filename="Real_acct_ownership_history.zip").exists()
            )
            self.assertTrue(
                Path(tmpdir, "var", "extracted", "Real_acct_owner", "payload.txt").exists()
            )

    def test_download_and_extract_hcad_raises_for_missing_required_archive(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            override_settings(**self.runtime_overrides(tmpdir)),
        ):
            with (
                patch("counties.harris.tasks_new.datetime", self.mock_datetime),
                patch(
                    "counties.harris.tasks_new.requests.get",
                    side_effect=self.fake_get_with_required_missing,
                ),
            ):
                with self.assertRaises(requests.HTTPError):
                    download_and_extract_hcad.run()

        self.assertFalse(DownloadRecord.objects.filter(filename="Real_acct_owner.zip").exists())


class AuthoritativeTaskDelegationTests(TestCase):
    @patch("counties.harris.etl_pipeline.run_harris_import")
    def test_celery_adapter_maps_legacy_flags_to_one_request(self, mocked_import):
        result = Mock(
            success=True,
            status=HarrisImportStatus.COMPLETED,
            errors=(),
            operation_id=ImportOperation.objects.create(
                county="harris", intent="preview", status="completed"
            ).pk,
            candidate_id="candidate-123",
            wrote_data=False,
            already_applied=False,
        )
        result.to_dict.return_value = {"status": "completed"}
        mocked_import.return_value = result

        _run_authoritative_pipeline(
            task_instance=None,
            skip_download=True,
            skip_extract=True,
            skip_load=True,
            data_year=2025,
            scope="building-only",
            strict=False,
        )

        # Serialized result keys are pinned against real runs in test_import_command_results.
        request = mocked_import.call_args.args[0]
        self.assertEqual(request.plan, HarrisImportPlan.from_legacy_scope("building-only"))
        self.assertEqual(request.data_year, 2025)
        self.assertIs(request.acquisition, HarrisAcquisitionMode.REUSE_DOWNLOADED)
        self.assertIs(request.extraction, HarrisExtractionMode.REUSE_EXTRACTED)
        self.assertIsInstance(request.load, HarrisPreview)
        self.assertIs(request.failure_policy, HarrisFailurePolicy.BEST_EFFORT)

    @patch("counties.harris.tasks_new._run_authoritative_pipeline")
    def test_legacy_building_task_delegates_to_building_plan(self, mocked_run):
        mocked_run.return_value = {"status": "completed"}

        result = download_and_import_building_data.run()

        self.assertEqual(result["status"], "completed")
        mocked_run.assert_called_once()
        _, kwargs = mocked_run.call_args
        self.assertEqual(kwargs["plan"], HarrisImportPlan.from_legacy_scope("building-only"))
        self.assertTrue(kwargs["strict"])

    @patch("counties.harris.tasks_new._run_authoritative_pipeline")
    def test_legacy_gis_task_delegates_to_gis_plan(self, mocked_run):
        mocked_run.return_value = {"status": "completed"}

        result = download_and_import_gis_data.run()

        self.assertEqual(result["status"], "completed")
        mocked_run.assert_called_once()
        _, kwargs = mocked_run.call_args
        self.assertEqual(kwargs["plan"], HarrisImportPlan.from_legacy_scope("gis-only"))
        self.assertTrue(kwargs["strict"])

    @patch("counties.harris.tasks_new._run_authoritative_pipeline")
    def test_run_etl_pipeline_propagates_pipeline_failure(self, mocked_run):
        mocked_run.side_effect = RuntimeError("pipeline failed")

        with self.assertRaises(RuntimeError):
            run_etl_pipeline.run()
