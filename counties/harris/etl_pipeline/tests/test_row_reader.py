"""Tests for the row_reader module — the single source of truth for parsing
HCAD source files into typed rows.

Covers column resolution, address assembly, the residential filter, the
fixtures-based bed/bath precedence, and the QUOTE_NONE quoting policy that
prevents embedded quotes from corrupting row boundaries.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from counties.harris.etl_pipeline.row_reader import (
    BuildingRow,
    ExtraFeatureRow,
    PropertyRow,
    iter_building_rows,
    iter_extra_feature_rows,
    iter_property_rows,
)


class _Fixtures:
    def __init__(self, bedrooms=0, bathrooms=0.0, half_baths=0):
        self._bedrooms = bedrooms
        self._bathrooms = bathrooms
        self._half_baths = half_baths

    def get_bedroom_count(self, acct, bnum):
        return self._bedrooms

    def get_bathroom_count(self, acct, bnum):
        return self._bathrooms

    def get_fixtures(self, acct, bnum):
        return {"half_baths": self._half_baths}


def _write(content: str) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="latin-1", suffix=".txt")
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


class PropertyRowTests(SimpleTestCase):
    def test_residential_row_is_parsed(self) -> None:
        path = _write(
            "acct\tmailto\tstr_num\tstr\tstr_sfx\tsite_addr_1\tsite_addr_2\tsite_addr_3\tstate_class\ttot_appr_val\tassessed_val\tbld_ar\tland_ar\n"
            "R1\tDOE JOHN\t100\tMAIN\tST\t\tHouston\t77001\tA1\t250000\t240000\t1800\t6000\n"
        )
        self.addCleanup(path.unlink)

        results = list(iter_property_rows(path))

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertFalse(result.skip)
        self.assertFalse(result.invalid)
        assert isinstance(result.row, PropertyRow)
        row = result.row
        self.assertEqual(row.account_number, "R1")
        self.assertEqual(row.address, "100 MAIN ST")
        self.assertEqual(row.city, "Houston")
        self.assertEqual(row.zipcode, "77001")
        self.assertEqual(row.owner_name, "DOE JOHN")
        self.assertEqual(row.value, 250000.0)
        self.assertEqual(row.assessed_value, 240000.0)
        self.assertEqual(row.building_area, 1800.0)
        self.assertEqual(row.land_area, 6000.0)
        self.assertEqual(row.state_class, "A1")
        self.assertTrue(row.is_residential)
        self.assertFalse(row.is_data_ready)

    def test_site_addr_wins_over_components(self) -> None:
        path = _write(
            "acct\tstr_num\tstr\tsite_addr_1\tsite_addr_3\tstate_class\n"
            "R2\t200\tELM\t200 ELM AVE\t77002\tA2\n"
        )
        self.addCleanup(path.unlink)

        results = list(iter_property_rows(path))
        assert isinstance(results[0].row, PropertyRow)
        self.assertEqual(results[0].row.address, "200 ELM AVE")

    def test_non_residential_and_blank_account_are_skipped(self) -> None:
        path = _write(
            "acct\tstr_num\tstr\tstate_class\n"
            "C1\t1\tCOMMERCE\tF1\n"
            "\t\t\tA1\n"
            "R3\t3\tOAK\tA1\n"
        )
        self.addCleanup(path.unlink)

        results = list(iter_property_rows(path))

        self.assertEqual(len(results), 3)
        self.assertTrue(results[0].skip)
        self.assertTrue(results[1].skip)
        self.assertFalse(results[2].skip)
        assert isinstance(results[2].row, PropertyRow)
        self.assertEqual(results[2].row.account_number, "R3")


class BuildingRowTests(SimpleTestCase):
    def test_fixtures_win_over_file_columns(self) -> None:
        path = _write(
            "acct\tbld_num\timprv_type\tbldg_class\tqa_cd\tcndtn_cd\tdate_erected\theat_ar\tsty\tbed_rm\tfull_bath\thalf_bath\n"
            "B1\t1\tA1\tR3\tA\tG\t1995\t1800\t1\t3\t2\t0\n"
        )
        self.addCleanup(path.unlink)

        account_map = {"B1": 42}
        fixtures = _Fixtures(bedrooms=4, bathrooms=2.5, half_baths=1)

        results = list(iter_building_rows(path, account_map, fixtures))

        self.assertEqual(len(results), 1)
        assert isinstance(results[0].row, BuildingRow)
        row = results[0].row
        self.assertEqual(row.property_id, 42)
        self.assertEqual(row.building_number, 1)
        self.assertEqual(row.year_built, 1995)
        self.assertEqual(row.heat_area, 1800.0)
        self.assertEqual(row.bedrooms, 4)
        self.assertEqual(row.bathrooms, 2.5)
        self.assertEqual(row.half_baths, 1)
        self.assertTrue(row.is_active)

    def test_file_columns_used_when_fixtures_absent(self) -> None:
        path = _write("acct\tbld_num\tbed_rm\tfull_bath\thalf_bath\n" "B1\t1\t3\t2\t1\n")
        self.addCleanup(path.unlink)

        account_map = {"B1": 42}
        fixtures = _Fixtures()  # all zeros -> fall back to file columns

        results = list(iter_building_rows(path, account_map, fixtures))

        assert isinstance(results[0].row, BuildingRow)
        row = results[0].row
        self.assertEqual(row.bedrooms, 3)
        self.assertEqual(row.bathrooms, 2.5)  # 2 full + 1 half * 0.5
        self.assertEqual(row.half_baths, 1)

    def test_missing_account_is_invalid(self) -> None:
        path = _write("acct\tbld_num\n" "MISSING\t1\n")
        self.addCleanup(path.unlink)

        results = list(iter_building_rows(path, {"B1": 42}, _Fixtures()))

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].invalid)
        self.assertIsNone(results[0].row)


class ExtraFeatureRowTests(SimpleTestCase):
    def test_detail_columns_are_parsed(self) -> None:
        path = _write(
            "acct\tcd\tdscr\tgrade\tcond_cd\tbld_num\tlength\twidth\tunits\tact_yr\tasd_val\n"
            "E1\tRRP5\tGunite Pool\t4\tA\t0\t21\t11\t231.00\t2021\t10589\n"
        )
        self.addCleanup(path.unlink)

        results = list(iter_extra_feature_rows(path, {"E1": 7}))

        self.assertEqual(len(results), 1)
        assert isinstance(results[0].row, ExtraFeatureRow)
        row = results[0].row
        self.assertEqual(row.property_id, 7)
        self.assertEqual(row.feature_code, "RRP5")
        self.assertEqual(row.feature_description, "Gunite Pool")
        self.assertEqual(row.quantity, 231.0)
        self.assertEqual(row.length, 21.0)
        self.assertEqual(row.width, 11.0)
        self.assertEqual(row.condition_code, "A")
        self.assertEqual(row.year_built, 2021)
        self.assertEqual(row.value, 10589.0)

    def test_fallback_file_uses_long_description(self) -> None:
        path = _write(
            "acct\tbld_num\tcount\tgrade\tcd\ts_dscr\tl_dscr\tcat\tdscr\tnote\tuts\n"
            "E2\t0\t1\t4\tCPA1\tPavAsp\tPaving - Asphalt\tMS\tMisc\t\t5000.00\n"
        )
        self.addCleanup(path.unlink)

        results = list(iter_extra_feature_rows(path, {"E2": 8}))

        assert isinstance(results[0].row, ExtraFeatureRow)
        row = results[0].row
        self.assertEqual(row.feature_description, "Paving - Asphalt")
        self.assertEqual(row.quantity, 1.0)
        self.assertEqual(row.value, 5000.0)


class QuotingTests(SimpleTestCase):
    def test_embedded_quote_does_not_corrupt_rows(self) -> None:
        """A field containing an unbalanced " must not swallow the next row."""
        path = _write(
            "acct\tstr_num\tstr\tsite_addr_1\tsite_addr_3\ttot_appr_val\tstate_class\n"
            "R1\t100\tMAIN ST\t100 MAIN ST\t77001\t250000\tA1\n"
            'R2\t200\tOAK "AVE"\t200 OAK "AVE"\t77002\t350000\tA2\n'
            "R3\t300\tELM ST\t300 ELM ST\t77003\t450000\tA1\n"
        )
        self.addCleanup(path.unlink)

        results = list(iter_property_rows(path))

        self.assertEqual(len(results), 3)
        assert isinstance(results[1].row, PropertyRow)
        self.assertEqual(results[1].row.address, '200 OAK "AVE"')
        assert isinstance(results[2].row, PropertyRow)
        self.assertEqual(results[2].row.account_number, "R3")
