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
                "acct\tbld_num\tbed_rm\tfull_bath\thalf_bath\n" "P1\t2\t3\t1.5\t1\n",
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

    def test_repeated_feature_identities_are_numbered_in_source_order_across_files(self) -> None:
        header = "acct\tbld_num\tcd\tlength\n"
        with TemporaryDirectory() as directory:
            first = self._write(
                directory,
                "extra_features_detail1.txt",
                header + "P1\t0\tRRS1\t10\nP1\t0\tRRS1\t13\nNOPE\t0\tRRS1\t9\nP1\t0\tRRC1\t20\n",
            )
            second = self._write(
                directory, "extra_features_detail2.txt", header + "P1\t0\tRRS1\t8\n"
            )
            ordinals: dict[tuple[str, str, int], int] = {}
            rows = [
                row
                for path in (first, second)
                for row in iter_extra_feature_rows(path, {"P1": 42}, ordinals=ordinals)
                if row.is_loadable
            ]

        self.assertEqual(
            [
                (record["feature_code"], record["length"], record["source_ordinal"])
                for record in (row.as_dict() for row in rows)
            ],
            [("RRS1", 10.0, 1), ("RRS1", 13.0, 2), ("RRC1", 20.0, 1), ("RRS1", 8.0, 3)],
        )

    def test_rows_that_do_not_match_the_header_are_invalid_rather_than_read_shifted(self) -> None:
        # A stray tab in the owner's mailing address shifts every later field one column right.
        with TemporaryDirectory() as directory:
            property_path = self._write(
                directory,
                "real_acct.txt",
                "acct\tmailto\tsite_addr_1\tstate_class\ttot_appr_val\n"
                "P1\tOwner\t\t100 MAIN ST\tA1\t250000\n"
                "\n",
            )
            building_path = self._write(
                directory, "building_res.txt", "acct\tbld_num\theat_ar\nP1\t1\n"
            )
            # A line break inside dscr splits one feature record in two.
            feature_path = self._write(
                directory,
                "extra_features_detail2.txt",
                "acct\tcd\tdscr\tbld_num\tasd_val\nP1\tCKGK\tTank\n\t0\t42592\n",
            )
            property_rows = list(iter_property_rows(property_path))
            building_rows = list(iter_building_rows(building_path, {"P1": 42}, _EmptyFixtures()))
            feature_rows = list(iter_extra_feature_rows(feature_path, {"P1": 42}))

        self.assertEqual([row.invalid for row in property_rows], [True, False])
        self.assertTrue(property_rows[1].skip)
        self.assertEqual([row.invalid for row in building_rows], [True])
        self.assertEqual([row.invalid for row in feature_rows], [True, True])
