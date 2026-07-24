"""Tests for FixturesAggregator's None-vs-zero fixture handling.

Regression coverage for a bug where an explicit RMB/RMF/RMH fixture row of
units=0.00 (a real zero-bedroom studio or zero-bath building) was
indistinguishable from "no fixture row for this type at all", because the
aggregator defaulted missing fields to 0.0 instead of None.
"""

import tempfile
import unittest
from pathlib import Path

from data.etl_pipeline.fixtures_aggregator import FixturesAggregator


class FixturesAggregatorZeroVsMissingTests(unittest.TestCase):
    def _load(self, lines: list[str]) -> FixturesAggregator:
        agg = FixturesAggregator()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fixtures.txt"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            agg.load_fixtures_file(path)
        return agg

    def test_explicit_zero_bedroom_row_is_kept_as_real_zero(self) -> None:
        agg = self._load(
            [
                "acct\tbld_num\ttype\ttype_dscr\tunits",
                "A1\t1\tRMB\tRoom:  Bedroom\t0.00",
                "A1\t1\tRMF\tRoom:  Full Bath\t1.00",
                "A1\t1\tRMH\tRoom:  Half Bath\t1.00",
            ]
        )
        self.assertEqual(agg.get_bedroom_count("A1", 1), 0)
        self.assertEqual(agg.get_bathroom_count("A1", 1), 1.5)

    def test_missing_bedroom_row_returns_none_not_zero(self) -> None:
        agg = self._load(
            [
                "acct\tbld_num\ttype\ttype_dscr\tunits",
                "A2\t1\tRMB\tRoom:  Bedroom\t3.00",
            ]
        )
        # No RMF/RMH row at all for A2 -> genuinely unknown, not zero.
        self.assertIsNone(agg.get_bathroom_count("A2", 1))
        self.assertEqual(agg.get_bedroom_count("A2", 1), 3)

    def test_unknown_building_returns_none_for_everything(self) -> None:
        agg = self._load(
            [
                "acct\tbld_num\ttype\ttype_dscr\tunits",
                "A3\t1\tRMB\tRoom:  Bedroom\t2.00",
            ]
        )
        self.assertIsNone(agg.get_bedroom_count("UNKNOWN", 1))
        self.assertIsNone(agg.get_bathroom_count("UNKNOWN", 1))
        fixtures = agg.get_fixtures("UNKNOWN", 1)
        self.assertIsNone(fixtures["bedrooms"])
        self.assertIsNone(fixtures["full_baths"])
        self.assertIsNone(fixtures["half_baths"])

    def test_explicit_zero_half_bath_only_yields_zero_not_none(self) -> None:
        agg = self._load(
            [
                "acct\tbld_num\ttype\ttype_dscr\tunits",
                "A4\t1\tRMH\tRoom:  Half Bath\t0.00",
            ]
        )
        self.assertEqual(agg.get_bathroom_count("A4", 1), 0.0)

    def test_get_stats_does_not_crash_on_none_fields(self) -> None:
        agg = self._load(
            [
                "acct\tbld_num\ttype\ttype_dscr\tunits",
                "A5\t1\tRMB\tRoom:  Bedroom\t0.00",
                "A6\t1\tRMF\tRoom:  Full Bath\t2.00",
            ]
        )
        stats = agg.get_stats()
        self.assertEqual(stats["total_buildings"], 2)
        self.assertEqual(stats["with_bedrooms"], 0)  # A5's explicit 0 doesn't count as "has bedrooms"
        self.assertEqual(stats["with_full_baths"], 1)


if __name__ == "__main__":
    unittest.main()
