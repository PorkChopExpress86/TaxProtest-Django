"""Contract tests pinning the Brazos CAD stage's inspect-then-persist interface."""

from __future__ import annotations

import hashlib
import json
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase

from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.models import (
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)
from counties.brazos.stage_contract import RefreshOptions
from counties.common.import_writers import county_writer
from counties.common.models import ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption

PROP_ID = "000000010013"
IMP_ID = "000000000001"
YEAR = 2026


def _fixed_line(length: int, fields: dict[tuple[int, int], str]) -> str:
    chars = [" "] * length
    for (start, end), value in fields.items():
        sliced = value[: end - start]
        chars[start : start + len(sliced)] = list(sliced)
    return "".join(chars)


def _info_line(prop_id: str = PROP_ID, year: int = YEAR, owner: str = "DOE JANE") -> str:
    return _fixed_line(
        987,
        {
            (0, 12): prop_id,
            (17, 22): f"0{year}",
            (608, 678): owner,
            (753, 873): "123 OAK ST",
            (873, 923): "BRYAN",
            (923, 925): "TX",
            (978, 983): "77801",
            (983, 987): "1234",
        },
    )


def _land_detail_line(
    prop_id: str = PROP_ID,
    year: int = YEAR,
    land_value_raw: str = "00000005000000",
) -> str:
    return _fixed_line(
        184,
        {
            (0, 12): prop_id,
            (12, 16): str(year),
            (16, 28): "000000000001",
            (28, 32): "A1",
            (38, 63): "RESIDENTIAL LOT",
            (140, 154): land_value_raw,
            (178, 184): "002500",
        },
    )


def _improvement_info_line(prop_id: str = PROP_ID, year: int = YEAR, imp_id: str = IMP_ID) -> str:
    return _fixed_line(
        49,
        {
            (0, 12): prop_id,
            (12, 16): str(year),
            (16, 28): imp_id,
            (28, 31): "R",
            (38, 49): "RESIDENTIAL",
        },
    )


def _improvement_detail_line(
    detail_seq: str,
    description: str,
    year_built: str,
    detail_value_raw: str,
    *,
    prop_id: str = PROP_ID,
    year: int = YEAR,
    imp_id: str = IMP_ID,
) -> str:
    return _fixed_line(
        622,
        {
            (0, 12): prop_id,
            (12, 16): str(year),
            (16, 28): imp_id,
            (28, 40): detail_seq,
            (50, 75): description,
            (85, 89): year_built,
            (93, 108): "00001800.000000",
            (108, 122): detail_value_raw,
            (122, 622): "R30,U60,L30,D60",
        },
    )


def _improvement_detail_attr_line(
    attribute_type: str,
    attribute_value: str,
    *,
    prop_id: str = PROP_ID,
    year: int = YEAR,
    imp_id: str = IMP_ID,
) -> str:
    return _fixed_line(
        87,
        {
            (0, 12): prop_id,
            (12, 16): str(year),
            (16, 28): imp_id,
            (28, 40): "000000000001",
            (52, 77): attribute_type,
            (77, 87): attribute_value,
        },
    )


def _entity_info_line(prop_id: str = PROP_ID, year: int = YEAR) -> str:
    return _fixed_line(
        418,
        {
            (0, 12): prop_id,
            (12, 17): f"0{year}",
            (41, 53): "000000237993",
            (53, 63): "G1",
            (63, 113): "BRAZOS COUNTY",
            (148, 163): "000000000250000",
            (163, 178): "000000000200000",
            (298, 313): "000000000050000",
            (313, 328): "000000000000000",
            (328, 343): "000000000000000",
            (388, 403): "000000000260000",
            (403, 418): "000000000250000",
        },
    )


def _write_qualifying_six_file_source(root: Path, year: int = YEAR) -> dict[str, Path]:
    extract_dir = root / "extracted" / str(year)
    extract_dir.mkdir(parents=True, exist_ok=True)
    duplicate_attr = _improvement_detail_attr_line("Fireplace", "BRICK", year=year)
    files_contents = {
        "APPRAISAL_INFO.TXT": [_info_line(year=year)],
        "APPRAISAL_LAND_DETAIL.TXT": [_land_detail_line(year=year)],
        "APPRAISAL_IMPROVEMENT_INFO.TXT": [_improvement_info_line(year=year)],
        "APPRAISAL_IMPROVEMENT_DETAIL.TXT": [
            _improvement_detail_line(
                "000000000001", "MAIN AREA", "1998", "0000200000.00", year=year
            ),
            _improvement_detail_line("000000000002", "GARAGE", "2005", "0000020000.00", year=year),
        ],
        "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT": [
            _improvement_detail_attr_line("Number of Bedrooms", "3", year=year),
            _improvement_detail_attr_line("Plumbing", "2 1/2", year=year),
            duplicate_attr,
            duplicate_attr,
        ],
        "APPRAISAL_ENTITY_INFO.TXT": [_entity_info_line(year=year)],
    }
    paths: dict[str, Path] = {}
    for filename, lines in files_contents.items():
        path = extract_dir / filename
        path.write_text("\r\n".join(lines) + "\r\n", encoding="latin-1")
        paths[filename] = path
    return paths


class CadStageTests(TestCase):
    def _options(self, year: int = YEAR) -> RefreshOptions:
        return RefreshOptions(tax_year=year, skip_download=True, skip_extract=True)

    def _operation(self) -> ImportOperation:
        return ImportOperation.objects.create(county="brazos", intent="cad_stage_test")

    def test_prepare_inspect_and_persist_qualifying_six_file_source(self):
        stage = CadRefreshStage()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _write_qualifying_six_file_source(root)
            operation = self._operation()

            with (
                county_writer(operation),
                self.settings(
                    BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                    BCAD_EXTRACT_DIR=str(root / "extracted"),
                ),
            ):
                preparation = stage.prepare(self._options())
                inspection = stage.inspect(preparation, operation)

                self.assertFalse(PropertyAccount.objects.exists())
                self.assertFalse(PropertyJurisdictionExemption.objects.exists())

                persisted = stage.persist(preparation)

                expected_digests = {
                    name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in paths.items()
                }
                expected_working_paths = {
                    name: str(path) for name, path in preparation.payload.text_files.items()
                }

        expected_cad_inspection = (
            '{"records_by_file": {'
            '"APPRAISAL_INFO.TXT": 1, '
            '"APPRAISAL_LAND_DETAIL.TXT": 1, '
            '"APPRAISAL_IMPROVEMENT_INFO.TXT": 1, '
            '"APPRAISAL_IMPROVEMENT_DETAIL.TXT": 2, '
            '"APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT": 4, '
            '"APPRAISAL_ENTITY_INFO.TXT": 1'
            '}, "duplicates_by_file": {'
            '"APPRAISAL_INFO.TXT": 0, '
            '"APPRAISAL_LAND_DETAIL.TXT": 0, '
            '"APPRAISAL_IMPROVEMENT_INFO.TXT": 0, '
            '"APPRAISAL_IMPROVEMENT_DETAIL.TXT": 0, '
            '"APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT": 1, '
            '"APPRAISAL_ENTITY_INFO.TXT": 0'
            "}}"
        )
        self.assertEqual(json.dumps(operation.evidence["cad_inspection"]), expected_cad_inspection)
        self.assertEqual(
            json.dumps(dict(inspection.metrics)),
            '{"APPRAISAL_INFO.TXT": 1, '
            '"APPRAISAL_LAND_DETAIL.TXT": 1, '
            '"APPRAISAL_IMPROVEMENT_INFO.TXT": 1, '
            '"APPRAISAL_IMPROVEMENT_DETAIL.TXT": 2, '
            '"APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT": 4, '
            '"APPRAISAL_ENTITY_INFO.TXT": 1}',
        )

        expected_persist_metrics = (
            '{"APPRAISAL_INFO.TXT": 1, '
            '"APPRAISAL_LAND_DETAIL.TXT": 1, '
            '"APPRAISAL_IMPROVEMENT_INFO.TXT": 1, '
            '"APPRAISAL_IMPROVEMENT_DETAIL.TXT": 2, '
            '"APPRAISAL_ENTITY_INFO.TXT": 2, '
            '"APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT": 2}'
        )
        self.assertEqual(json.dumps(dict(persisted.metrics)), expected_persist_metrics)

        expected_source_order = [
            "APPRAISAL_INFO.TXT",
            "APPRAISAL_LAND_DETAIL.TXT",
            "APPRAISAL_IMPROVEMENT_INFO.TXT",
            "APPRAISAL_IMPROVEMENT_DETAIL.TXT",
            "APPRAISAL_ENTITY_INFO.TXT",
            "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT",
        ]
        sources = operation.evidence["sources"]
        self.assertEqual([Path(item["path"]).name for item in sources], expected_source_order)
        for item in sources:
            filename = Path(item["path"]).name
            self.assertEqual(item["path"], expected_working_paths[filename])
            self.assertEqual(item["sha256"], expected_digests[filename])
            self.assertEqual(item["stage"], "cad")
            self.assertEqual(item["source_year"], YEAR)
            self.assertEqual(item["target_year"], YEAR)
            self.assertIsNone(item["source_url"])

        account = PropertyAccount.objects.get(prop_id=PROP_ID, tax_year=YEAR)
        self.assertEqual(account.owner_name, "DOE JANE")
        self.assertEqual(account.mailing_zip, "77801-1234")
        self.assertEqual(account.assessed_value, Decimal("250000"))

        land = PropertyLand.objects.get(prop_id=PROP_ID, tax_year=YEAR)
        self.assertEqual(land.land_value, Decimal("50000.00"))
        self.assertEqual(land.acreage, Decimal("0.2500"))

        improvement = PropertyImprovement.objects.get(prop_id=PROP_ID, tax_year=YEAR, imp_id=IMP_ID)
        self.assertEqual(improvement.year_built, 1998)
        self.assertEqual(PropertyImprovementDetail.objects.filter(tax_year=YEAR).count(), 2)

        characteristic = PropertyBuildingCharacteristic.objects.get(
            prop_id=PROP_ID, tax_year=YEAR, imp_id=IMP_ID
        )
        self.assertEqual(characteristic.bedrooms, 3)
        self.assertEqual(characteristic.bathrooms, Decimal("2"))
        self.assertEqual(characteristic.half_baths, 1)

        feature = PropertyExtraFeature.objects.get(prop_id=PROP_ID, tax_year=YEAR, imp_id=IMP_ID)
        self.assertEqual(feature.feature_type, "Fireplace")
        self.assertEqual(feature.feature_value, "BRICK")

        exemptions = PropertyJurisdictionExemption.objects.filter(
            county="brazos", account_number=PROP_ID, tax_year=YEAR
        ).order_by("exemption_code")
        self.assertEqual(
            [(row.exemption_code, row.taxable_value, row.exemption_amount) for row in exemptions],
            [
                ("", Decimal("250000"), None),
                ("HS", Decimal("200000"), Decimal("50000")),
            ],
        )

    def test_preflight_and_inspection_rejection_messages(self):
        cases = (
            (
                "missing file",
                "CAD preflight missing required PACS files: APPRAISAL_ENTITY_INFO.TXT",
            ),
            (
                "short line",
                "CAD preflight failed PACS key/year integrity for APPRAISAL_INFO.TXT.",
            ),
            (
                "missing required field",
                "CAD preflight failed PACS key/year integrity for APPRAISAL_IMPROVEMENT_INFO.TXT.",
            ),
            (
                "wrong year",
                "CAD preflight failed PACS key/year integrity for APPRAISAL_INFO.TXT.",
            ),
            (
                "invalid property key",
                "Invalid canonical property key in APPRAISAL_INFO.TXT, row 1.",
            ),
            (
                "malformed numeric",
                "Malformed numeric land_value in APPRAISAL_LAND_DETAIL.TXT, row 1.",
            ),
            (
                "conflicting key",
                "Conflicting canonical key in APPRAISAL_INFO.TXT, row 2.",
            ),
        )
        stage = CadRefreshStage()

        for kind, expected_message in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = _write_qualifying_six_file_source(root)
                if kind == "missing file":
                    paths["APPRAISAL_ENTITY_INFO.TXT"].unlink()
                elif kind == "short line":
                    paths["APPRAISAL_INFO.TXT"].write_text(
                        _info_line()[:50] + "\r\n", encoding="latin-1"
                    )
                elif kind == "missing required field":
                    paths["APPRAISAL_IMPROVEMENT_INFO.TXT"].write_text(
                        _improvement_info_line(imp_id="") + "\r\n", encoding="latin-1"
                    )
                elif kind == "wrong year":
                    paths["APPRAISAL_INFO.TXT"].write_text(
                        _info_line(year=YEAR - 1) + "\r\n", encoding="latin-1"
                    )
                elif kind == "invalid property key":
                    paths["APPRAISAL_INFO.TXT"].write_text(
                        _info_line(prop_id="ABC") + "\r\n", encoding="latin-1"
                    )
                elif kind == "malformed numeric":
                    paths["APPRAISAL_LAND_DETAIL.TXT"].write_text(
                        _land_detail_line(land_value_raw="BAD") + "\r\n", encoding="latin-1"
                    )
                elif kind == "conflicting key":
                    paths["APPRAISAL_INFO.TXT"].write_text(
                        _info_line(owner="DOE JANE")
                        + "\r\n"
                        + _info_line(owner="DOE JOHN")
                        + "\r\n",
                        encoding="latin-1",
                    )

                operation = self._operation()
                with (
                    county_writer(operation),
                    self.settings(
                        BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                        BCAD_EXTRACT_DIR=str(root / "extracted"),
                    ),
                    self.assertRaises(CommandError) as exc_info,
                ):
                    preparation = stage.prepare(self._options())
                    stage.inspect(preparation, operation)

                self.assertEqual(str(exc_info.exception), expected_message)
                self.assertNotIn("cad_inspection", operation.evidence)
                self.assertFalse(PropertyAccount.objects.exists())

    def test_persist_rejects_wrong_year_row_without_preflight(self):
        wrong_year_lines = {
            "APPRAISAL_INFO.TXT": _info_line(year=YEAR - 1),
            "APPRAISAL_LAND_DETAIL.TXT": _land_detail_line(year=YEAR - 1),
            "APPRAISAL_IMPROVEMENT_INFO.TXT": _improvement_info_line(year=YEAR - 1),
            "APPRAISAL_IMPROVEMENT_DETAIL.TXT": _improvement_detail_line(
                "000000000001", "MAIN AREA", "1998", "0000200000.00", year=YEAR - 1
            ),
            "APPRAISAL_ENTITY_INFO.TXT": _entity_info_line(year=YEAR - 1),
            "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT": _improvement_detail_attr_line(
                "Fireplace", "BRICK", year=YEAR - 1
            ),
        }
        stage = CadRefreshStage()

        for filename, wrong_line in wrong_year_lines.items():
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = _write_qualifying_six_file_source(root)
                paths[filename].write_text(wrong_line + "\r\n", encoding="latin-1")

                operation = self._operation()
                with (
                    county_writer(operation),
                    self.settings(
                        BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                        BCAD_EXTRACT_DIR=str(root / "extracted"),
                    ),
                ):
                    preparation = stage.prepare(self._options())
                    with self.assertRaises(CommandError) as exc_info, transaction.atomic():
                        stage.persist(preparation)

                self.assertIn(filename, str(exc_info.exception))
                self.assertFalse(PropertyAccount.objects.exists())
