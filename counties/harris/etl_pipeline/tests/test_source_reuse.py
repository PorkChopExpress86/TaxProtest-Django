"""One import reads each Harris source file once and keeps an unchanged extract."""

import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from counties.common import import_source_digests as source_digests
from counties.common.models import ImportOperation
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisPreview,
    run_harris_import,
)
from counties.harris.etl_pipeline.config import ETLConfig
from counties.harris.etl_pipeline.extract import ExtractManager
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

PROPERTY_ROWS = (
    "acct\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val\n"
    "P100\t100 MAIN ST\t77001\tA1\t250000\n"
)
REAL_ACCOUNT_OWNER = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_ACCOUNT_OWNER)


def _write_archive(path: Path, rows: str = PROPERTY_ROWS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("real_acct.txt", rows)
        archive.writestr("owners.txt", "acct\tname\nP100\tOWNER\n")


def _counting_hashes():
    hashed: Counter[Path] = Counter()
    real_hash = source_digests._hash

    def counted(path: Path) -> str:
        hashed[Path(path)] += 1
        return real_hash(path)

    return hashed, patch.object(source_digests, "_hash", side_effect=counted)


class OneImportDigestTests(TestCase):
    def request(self) -> HarrisImportRequest:
        return HarrisImportRequest(
            plan=HarrisImportPlan.from_legacy_scope("property-only"),
            data_year=2026,
            acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
            extraction=HarrisExtractionMode.EXTRACT,
            load=HarrisPreview(),
        )

    def test_one_import_computes_one_digest_per_source_file(self):
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            archive = Path(root) / "downloads" / REAL_ACCOUNT_OWNER.filename
            _write_archive(archive)
            hashed, counting = _counting_hashes()
            with counting:
                result = run_harris_import(self.request())

            self.assertTrue(result.success, result.errors)
            operation = ImportOperation.objects.get(pk=result.operation_id)
            extracted = Path(operation.evidence["working_sources"][1]) / (
                DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(HcadSourceId.REAL_ACCOUNT_OWNER)
            )
            # The working copy of the archive takes the digest read from the base root;
            # only the files extraction wrote are new to this import.
            self.assertEqual(
                hashed,
                Counter([archive, extracted / "real_acct.txt", extracted / "owners.txt"]),
            )
            recorded = {
                source["path"]: source["sha256"] for source in operation.evidence["sources"]
            }
            self.assertEqual(
                recorded[str(Path(operation.evidence["working_sources"][0]) / archive.name)],
                source_digests._hash(archive),
            )

    def test_a_second_import_of_an_unchanged_archive_does_not_extract_again(self):
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_archive(Path(root) / "downloads" / REAL_ACCOUNT_OWNER.filename)
            first = run_harris_import(self.request())
            self.assertTrue(first.success, first.errors)

            with patch.object(
                ExtractManager, "_extract_zip", side_effect=AssertionError("extracted again")
            ):
                second = run_harris_import(self.request())

            self.assertTrue(second.success, second.errors)
            operation = ImportOperation.objects.get(pk=second.operation_id)
            self.assertEqual(
                operation.evidence["reused_from"][str(Path(root) / "extracted")],
                str(first.operation_id),
            )


class UnchangedExtractTests(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = ETLConfig(
            download_dir=self.root / "downloads",
            extract_dir=self.root / "extracted",
            log_dir=self.root / "logs",
        )
        self.archive = self.config.download_dir / REAL_ACCOUNT_OWNER.filename
        _write_archive(self.archive)
        self.manager = ExtractManager(self.config)
        first = self.manager.extract_archive(REAL_ACCOUNT_OWNER)
        self.assertTrue(first.success, first.error)
        self.extract = self.manager.get_extract_path(REAL_ACCOUNT_OWNER)

    def extract_again(self):
        real_extract = ExtractManager._extract_zip
        with patch.object(
            ExtractManager, "_extract_zip", autospec=True, side_effect=real_extract
        ) as extracting:
            result = self.manager.extract_archive(REAL_ACCOUNT_OWNER)
        self.assertTrue(result.success, result.error)
        return extracting.called, result

    def test_an_unchanged_archive_with_a_complete_extract_is_not_extracted_again(self):
        extracted, result = self.extract_again()

        self.assertFalse(extracted)
        self.assertEqual(sorted(result.files_extracted), ["owners.txt", "real_acct.txt"])
        self.assertEqual(result.extract_dir, self.extract)

    def test_a_changed_archive_is_extracted_again(self):
        _write_archive(self.archive, PROPERTY_ROWS + "P200\t200 MAIN ST\t77001\tA1\t1\n")

        extracted, _ = self.extract_again()

        self.assertTrue(extracted)
        self.assertIn("P200", (self.extract / "real_acct.txt").read_text())

    def test_an_extract_missing_a_file_is_extracted_again(self):
        (self.extract / "owners.txt").unlink()

        extracted, _ = self.extract_again()

        self.assertTrue(extracted)
        self.assertTrue((self.extract / "owners.txt").is_file())

    def test_an_extract_whose_file_changed_is_extracted_again(self):
        (self.extract / "real_acct.txt").write_text("truncated")

        extracted, _ = self.extract_again()

        self.assertTrue(extracted)
        self.assertEqual((self.extract / "real_acct.txt").read_text(), PROPERTY_ROWS)

    def test_an_extract_without_its_provenance_is_extracted_again(self):
        (self.extract / source_digests.PROVENANCE_FILENAME).unlink()

        extracted, _ = self.extract_again()

        self.assertTrue(extracted)

    def test_an_extract_of_a_different_member_selection_is_extracted_again(self):
        self.manager.extract_config.allowed_extensions = [".txt", ".csv"]

        extracted, _ = self.extract_again()

        self.assertTrue(extracted)

    def test_an_interrupted_extraction_leaves_no_provenance_to_reuse(self):
        _write_archive(self.archive, PROPERTY_ROWS + "P200\t200 MAIN ST\t77001\tA1\t1\n")
        with patch.object(ExtractManager, "_extract_zip", side_effect=OSError("disk full")):
            failed = self.manager.extract_archive(REAL_ACCOUNT_OWNER)
        self.assertFalse(failed.success)

        self.assertFalse((self.extract / source_digests.PROVENANCE_FILENAME).exists())
        extracted, _ = self.extract_again()
        self.assertTrue(extracted)

    def test_a_left_over_partial_provenance_is_not_a_source(self):
        (self.extract / f"{source_digests.PROVENANCE_FILENAME}.partial").write_text("{")

        self.assertEqual(
            sorted(source_digests.tree_digests(self.extract)), ["owners.txt", "real_acct.txt"]
        )

    def test_a_missing_archive_fails_without_removing_its_extract(self):
        self.archive.unlink()

        result = self.manager.extract_archive(REAL_ACCOUNT_OWNER)

        self.assertFalse(result.success)
        self.assertTrue((self.extract / "real_acct.txt").is_file())
