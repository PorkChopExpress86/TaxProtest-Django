"""Unit tests for the fixed-width BCAD parsers in brazos_cad/parsers/pacs.py.

Fixtures are hand-built fixed-width strings using the verified byte offsets
(see the module docstring in parsers/pacs.py) — no DB, fast, and the durable
regression protection against future BCAD layout drift.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from brazos_cad.parsers.pacs import (
    parse_entity_info_line,
    parse_entity_line,
    parse_improvement_detail_attr_line,
    parse_improvement_detail_line,
    parse_improvement_info_line,
    parse_info_line,
    parse_land_detail_line,
)


def _line(length: int, fields: dict[tuple[int, int], str]) -> str:
    chars = [" "] * length
    for (start, end), value in fields.items():
        value = value[: end - start]
        chars[start : start + len(value)] = list(value)
    return "".join(chars)


class ParseLandDetailLineTests(SimpleTestCase):
    def test_parses_prop_id_and_money_and_acreage(self):
        line = _line(
            199,
            {
                (0, 12): "000000010002",
                (12, 16): "2025",
                (16, 28): "000000100000",
                (28, 32): "A1",
                (38, 63): "IMPROVED PASTURE",
                (140, 154): "00000002833677",
                (178, 184): "105024",
            },
        )
        parsed = parse_land_detail_line(line)
        self.assertEqual(parsed["prop_id"], "000000010002")
        self.assertEqual(parsed["tax_year"], 2025)
        self.assertEqual(parsed["land_seq"], 100000)
        self.assertEqual(parsed["land_use_code"], "A1")
        self.assertEqual(parsed["land_use_description"], "IMPROVED PASTURE")
        self.assertEqual(parsed["land_value"], Decimal("28336.77"))
        self.assertEqual(parsed["acreage"], Decimal("10.5024"))

    def test_blank_land_value_and_acreage_are_none(self):
        line = _line(199, {(0, 12): "000000010002", (12, 16): "2025", (16, 28): "000000100000"})
        parsed = parse_land_detail_line(line)
        self.assertIsNone(parsed["land_value"])
        self.assertIsNone(parsed["acreage"])


class ParseImprovementInfoLineTests(SimpleTestCase):
    def test_parses_core_fields(self):
        line = _line(
            114,
            {
                (0, 12): "000000010002",
                (12, 16): "2025",
                (16, 28): "000000100000",
                (28, 31): "R",
                (38, 49): "RESIDENTIAL",
            },
        )
        parsed = parse_improvement_info_line(line)
        self.assertEqual(parsed["prop_id"], "000000010002")
        self.assertEqual(parsed["imp_id"], "000000100000")
        self.assertEqual(parsed["improvement_type"], "R")
        self.assertEqual(parsed["improvement_description"], "RESIDENTIAL")
        # No improvement_value/square_feet fields — confirmed unreliable/zero
        # in the real export, not parsed at all.
        self.assertNotIn("improvement_value", parsed)
        self.assertNotIn("square_feet", parsed)


class ParseImprovementDetailLineTests(SimpleTestCase):
    def test_parses_literal_decimal_fields_and_long_tail(self):
        unit_text = "R30,U60,L30,D60"
        line = _line(
            622,
            {
                (0, 12): "000000010002",
                (12, 16): "2025",
                (16, 28): "000000100000",
                (28, 40): "000000937432",
                (50, 75): "METAL BUILDING",
                (85, 89): "1980",
                (93, 108): "00001800.000000",
                (108, 122): "0005630.000000",
                (122, 622): unit_text,
            },
        )
        parsed = parse_improvement_detail_line(line)
        self.assertEqual(parsed["prop_id"], "000000010002")
        self.assertEqual(parsed["imp_id"], "000000100000")
        self.assertEqual(parsed["detail_seq"], 937432)
        self.assertEqual(parsed["detail_description"], "METAL BUILDING")
        self.assertEqual(parsed["year_built"], 1980)
        self.assertEqual(parsed["detail_quantity"], Decimal("1800.000000"))
        self.assertEqual(parsed["detail_value"], Decimal("5630.000000"))
        self.assertEqual(parsed["detail_unit"], unit_text)

    def test_zero_year_built_is_none(self):
        line = _line(622, {(85, 89): "0000"})
        parsed = parse_improvement_detail_line(line)
        self.assertIsNone(parsed["year_built"])

    def test_long_detail_unit_up_to_500_chars_survives(self):
        long_text = "X" * 500
        line = _line(622, {(122, 622): long_text})
        parsed = parse_improvement_detail_line(line)
        self.assertEqual(parsed["detail_unit"], long_text)


class ParseImprovementDetailAttrLineTests(SimpleTestCase):
    def test_parses_attribute_type_and_value(self):
        line = _line(
            87,
            {
                (0, 12): "000000010008",
                (12, 16): "2025",
                (16, 28): "000000100002",
                (28, 40): "000000100001",
                (52, 77): "Fireplace",
                (77, 87): "G",
            },
        )
        parsed = parse_improvement_detail_attr_line(line)
        self.assertEqual(parsed["prop_id"], "000000010008")
        self.assertEqual(parsed["tax_year"], 2025)
        self.assertEqual(parsed["imp_id"], "000000100002")
        self.assertEqual(parsed["detail_seq"], 100001)
        self.assertEqual(parsed["attribute_type"], "Fireplace")
        self.assertEqual(parsed["attribute_value"], "G")


class ParseInfoLineTests(SimpleTestCase):
    def test_owner_name_internal_double_space_is_collapsed(self):
        """Regression guard: confirmed via live BCAD cross-check
        (validate_brazos_against_source) that the raw export genuinely
        contains double spaces mid-name (e.g. "MILLER NORMAN B  & LESLIE B")
        and BCAD's own live property search collapses them for display --
        matching that avoids a false-positive mismatch."""
        line = _line(9247, {(608, 678): "MILLER NORMAN B  & LESLIE B"})
        parsed = parse_info_line(line)
        self.assertEqual(parsed["owner_name"], "MILLER NORMAN B & LESLIE B")

    def test_parses_owner_and_address_and_combines_zip(self):
        line = _line(
            9247,
            {
                (0, 12): "000000010002",
                (17, 22): "02025",
                (608, 678): "STASNY FAMILY RANCH LLC",
                (753, 873): "7932 DRUMMER CIR",
                (873, 923): "COLLEGE STATION",
                (923, 925): "TX",
                (978, 983): "77845",
                (983, 987): "8087",
            },
        )
        parsed = parse_info_line(line)
        self.assertEqual(parsed["prop_id"], "000000010002")
        self.assertEqual(parsed["tax_year"], 2025)
        self.assertEqual(parsed["owner_name"], "STASNY FAMILY RANCH LLC")
        self.assertEqual(parsed["mailing_address"], "7932 DRUMMER CIR")
        self.assertEqual(parsed["mailing_city"], "COLLEGE STATION")
        self.assertEqual(parsed["mailing_state"], "TX")
        self.assertEqual(parsed["mailing_zip"], "77845-8087")

    def test_zip5_without_zip4_is_not_hyphenated(self):
        line = _line(9247, {(978, 983): "77845"})
        parsed = parse_info_line(line)
        self.assertEqual(parsed["mailing_zip"], "77845")


class ParseEntityLineTests(SimpleTestCase):
    def test_parses_id_and_code(self):
        line = _line(17, {(0, 12): "000000237982", (12, 17): "C1"})
        parsed = parse_entity_line(line)
        self.assertEqual(parsed["entity_id"], "000000237982")
        self.assertEqual(parsed["entity_code"], "C1")


class ParseEntityInfoLineTests(SimpleTestCase):
    def test_parses_prop_id_and_unscaled_amounts(self):
        line = _line(
            2750,
            {
                (0, 12): "000000010013",
                (12, 17): "02025",
                (41, 53): "000000237993",
                (53, 63): "G1",
                (63, 113): "BRAZOS COUNTY",
                (148, 163): "000000000242613",
                (163, 178): "000000000167613",
                (298, 313): "000000000000000",
                (313, 328): "000000000075000",
                (328, 343): "000000000000000",
            },
        )
        parsed = parse_entity_info_line(line)
        self.assertEqual(parsed["prop_id"], "000000010013")
        self.assertEqual(parsed["tax_year"], 2025)
        self.assertEqual(parsed["entity_id"], "000000237993")
        self.assertEqual(parsed["tax_unit_code"], "G1")
        self.assertEqual(parsed["tax_unit_name"], "BRAZOS COUNTY")
        self.assertEqual(parsed["assessed_value"], Decimal("242613"))
        self.assertEqual(parsed["taxable_value"], Decimal("167613"))
        self.assertEqual(parsed["hs_amt"], Decimal("0"))
        self.assertEqual(parsed["ov65_amt"], Decimal("75000"))
        self.assertEqual(parsed["dp_amt"], Decimal("0"))

    def test_blank_amounts_are_none_not_zero(self):
        line = _line(2750, {(0, 12): "000000010055"})
        parsed = parse_entity_info_line(line)
        self.assertIsNone(parsed["hs_amt"])
        self.assertIsNone(parsed["ov65_amt"])
        self.assertIsNone(parsed["dp_amt"])
        self.assertIsNone(parsed["assessed_value"])
