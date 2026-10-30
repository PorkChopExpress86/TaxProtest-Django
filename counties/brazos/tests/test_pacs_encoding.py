"""Every PACS reader decodes the fixed-width files the same, byte-preserving way.

PACS offsets are byte positions: every line of every file in the real 2026
export has one uniform byte width, and that export contains no byte above
0x7F. A single-byte decoding is the only one that keeps those offsets whatever
byte a future export carries; UTF-8 would turn a two-byte character into one
and shift every later field on the line.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from counties.brazos.cad_refresh import ENTITY_INFO_FILENAME, CadRefreshStage
from counties.brazos.management.commands.import_brazos_assessment_history import Command
from counties.brazos.parsers.pacs import PACS_ENCODING
from counties.common.tax_models import PropertyJurisdictionExemption


def _entity_info_bytes_with_a_two_byte_character() -> bytes:
    chars = [" "] * 2750
    for start, value in (
        (0, "000000010013"),
        (12, "02025"),
        (41, "000000237993"),
        (53, "G1"),
        (63, "NAVASOTA COUNTY"),
        (148, "000000000242613"),
        (163, "000000000242613"),
        (388, "000000000676378"),
        (403, "000000000243298"),
    ):
        chars[start : start + len(value)] = list(value)
    raw = bytearray("".join(chars).encode("ascii"))
    # "Ñ" in UTF-8 occupies two of the tax-unit name's fixed bytes.
    raw[67:69] = "Ñ".encode()
    return bytes(raw) + b"\r\n"


class PacsEncodingTests(TestCase):
    def _write(self, tmp: str) -> Path:
        path = Path(tmp) / ENTITY_INFO_FILENAME
        path.write_bytes(_entity_info_bytes_with_a_two_byte_character())
        return path

    def test_the_shared_encoding_is_single_byte(self):
        self.assertEqual(PACS_ENCODING, "latin-1")

    def test_cad_reader_keeps_later_offsets_after_a_non_ascii_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            CadRefreshStage()._load_entity_info(self._write(tmp), 2025, dry_run=False)

        base = PropertyJurisdictionExemption.objects.get(
            account_number="000000010013", tax_year=2025, county="brazos", exemption_code=""
        )
        self.assertEqual(base.assessed_value, Decimal("242613"))
        self.assertEqual(base.taxable_value, Decimal("242613"))

    def test_history_reader_keeps_later_offsets_after_a_non_ascii_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            [rollup] = Command()._roll_up_year(self._write(tmp), 2025, accounts=None)

        self.assertEqual(rollup.assessed_value, Decimal("242613"))
        self.assertEqual(rollup.market_value, Decimal("676378"))
        self.assertEqual(rollup.appraised_value, Decimal("243298"))
