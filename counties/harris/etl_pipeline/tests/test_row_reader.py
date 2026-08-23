"""Contract tests for the shared Harris row-translation module."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from counties.harris.etl_pipeline.row_reader import (
    iter_building_rows,
    iter_extra_feature_rows,
    iter_property_rows,
)


class _EmptyFixtures:
    def get_bedroom_count(self, account_number: str, building_number: int) -> int:
        return 0

    def get_bathroom_count(self, account_number: str, building_number: int) -> float:
        return 0.0

    def get_fixtures(self, account_number: str, building_number: int) -> dict[str, float]:
        return {"bedrooms": 0.0, "full_baths": 0.0, "half_baths": 0.0}


class RowReaderTests(SimpleTestCase):
    def _write(self, directory: str, filename: str, contents: str) -> Path:
        path = Path(directory) / filename
        path.write_text(contents, encoding="latin-1")
        return path

    def test_property_contract_unifies_aliases_quotes_and_field_lengths(self) -> None:
        with TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "real_acct.txt",
                "account_num\tmailto\tstr_num\tstr\tstr_sfx\tsite_addr_1\tsite_addr_2\t"
                "site_addr_3\tstate_class\ttot_appr_val\n"
                'P1\tOwner "One"\t100\tMAIN\tST\t100 MAIN ST\tHouston\t'
                "12345678901234567890\tA1\t$250,000\n"
                "COMM\tCommercial\t1\tCOMMERCE\tST\t\tHouston\t77001\tF1\t900000\n"
                "\tNo Account\t\t\t\t\t\t\tA1\t\n",
            )

            rows = list(iter_property_rows(path))

        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0].is_loadable)
        self.assertTrue(rows[1].skip)
        self.assertTrue(rows[2].skip)
        record = rows[0].as_dict()
        self.assertEqual(record["account_number"], "P1")
        self.assertEqual(record["owner_name"], 'Owner "One"')
        self.assertEqual(record["zipcode"], "12345678901234567890")
        self.assertEqual(record["address"], "100 MAIN ST")
        self.assertEqual(record["value"], 250000.0)

    def test_building_contract_preserves_fractional_full_baths(self) -> None:
        with TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "building_res.txt",
                "acct\tbld_num\tbed_rm\tfull_bath\thalf_bath\n"
                "P1\t2\t3\t1.5\t1\n",
            )
            rows = list(iter_building_rows(path, {"P1": 42}, _EmptyFixtures()))

        record = rows[0].as_dict()
        self.assertEqual(record["property_id"], 42)
        self.assertEqual(record["building_number"], 2)
        self.assertEqual(record["bedrooms"], 3)
        self.assertEqual(record["bathrooms"], 2.0)
        self.assertEqual(record["half_baths"], 1)

    def test_extra_feature_contract_preserves_area(self) -> None:
        with TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "extra_features_detail1.txt",
                "acct\tbld_num\tcd\tdscr\tgrade\tcond_cd\tarea\tlength\twidth\tunits\tact_yr\tasd_val\n"
                "P1\t0\tRRP5\tGunite Pool\t4\tA\t231\t21\t11\t1\t2021\t10589\n",
            )
            rows = list(iter_extra_feature_rows(path, {"P1": 42}))

        record = rows[0].as_dict()
        self.assertEqual(record["feature_description"], "Gunite Pool")
        self.assertEqual(record["area"], 231.0)
        self.assertEqual(record["value"], 10589.0)
