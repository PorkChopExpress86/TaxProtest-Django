"""Tests for import_brazos_assessment_history (issue #13).

Fixtures use the same hand-built fixed-width line convention as
test_load_brazos_cad.py, plus the real values verified against the 2025 and
2022 BCAD exports for market_value/appraised_value/assessed_value (see
parsers/pacs.py's ENTITY_INFO_LAYOUT docstring).
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from counties.brazos.management.commands.import_brazos_assessment_history import (
    ENTITY_INFO_FILENAME,
    Command,
    YearRollup,
)
from counties.brazos.models import PropertyAccount
from counties.common.tax_models import AssessmentHistory


def _line(length: int, fields: dict[tuple[int, int], str]) -> str:
    chars = [" "] * length
    for (start, end), value in fields.items():
        value = value[: end - start]
        chars[start : start + len(value)] = list(value)
    return "".join(chars)


def _entity_info_line(
    prop_id: str,
    tax_year: str,
    tax_unit_code: str,
    *,
    assessed_raw: str = "000000000000000",
    market_raw: str = "000000000000000",
    appraised_raw: str = "000000000000000",
) -> str:
    return _line(
        2750,
        {
            (0, 12): prop_id,
            (12, 17): tax_year,
            (41, 53): "000000237993",
            (53, 63): tax_unit_code,
            (63, 113): "BRAZOS COUNTY",
            (148, 163): assessed_raw,
            (388, 403): market_raw,
            (403, 418): appraised_raw,
        },
    )


# Real 2025 values for the property used throughout this project's tests:
# market $676,378 / appraised $243,298 / assessed $242,613 (a $685 cap loss).
REAL_2025_ROW = _entity_info_line(
    "000000010013",
    "02025",
    "G1",
    assessed_raw="000000000242613",
    market_raw="000000000676378",
    appraised_raw="000000000243298",
)


class YearRollupTests(SimpleTestCase):
    def test_cap_account_is_y_when_appraised_exceeds_assessed(self):
        rollup = YearRollup("X", Decimal("242613"), Decimal("243298"), Decimal("676378"))
        self.assertEqual(rollup.cap_account, "Y")

    def test_cap_account_is_blank_when_equal(self):
        rollup = YearRollup("X", Decimal("100000"), Decimal("100000"), Decimal("500000"))
        self.assertEqual(rollup.cap_account, "")

    def test_cap_account_is_blank_when_either_value_missing(self):
        self.assertEqual(YearRollup("X", None, Decimal("1"), None).cap_account, "")
        self.assertEqual(YearRollup("X", Decimal("1"), None, None).cap_account, "")

    def test_sanely_ordered_true_for_market_ge_appraised_ge_assessed(self):
        rollup = YearRollup("X", Decimal("1"), Decimal("2"), Decimal("3"))
        self.assertTrue(rollup.is_sanely_ordered)

    def test_sanely_ordered_false_when_order_violated(self):
        rollup = YearRollup("X", Decimal("3"), Decimal("2"), Decimal("1"))
        self.assertFalse(rollup.is_sanely_ordered)

    def test_sanely_ordered_vacuously_true_when_a_value_is_missing(self):
        self.assertTrue(YearRollup("X", None, None, None).is_sanely_ordered)


class ListArchivesTests(SimpleTestCase):
    # Mirrors the real page structure confirmed live 2026-08-09: ten years
    # (2016-2025), one anchor per year, plain "Download <year> Certified
    # Data" link text -- no monthly/map-book noise like the GIS portal has.
    PORTAL_FIXTURE_HTML = """
    <html><body>
    <a href="/wp-content/uploads/2025/08/2025-CERTIFIED-EXPORT.zip">Download 2025 Certified Data</a>
    <a href="/wp-content/uploads/2024/08/2024-CERTIFICATION-EXPORT.zip">Download 2024 Certified Data</a>
    <a href="/wp-content/uploads/2023/08/2022-CERTIFIED-DATA-DOWNLOAD.zip">Download 2022 Certified Data</a>
    </body></html>
    """

    def _mock_response(self, html: str) -> MagicMock:
        resp = MagicMock()
        resp.text = html
        resp.raise_for_status = MagicMock()
        return resp

    def test_lists_every_year_found(self):
        with patch(
            "counties.brazos.management.commands.import_brazos_assessment_history.requests.get",
            return_value=self._mock_response(self.PORTAL_FIXTURE_HTML),
        ):
            archives = Command()._list_archives("https://brazoscad.org/certified-data-downloads/")

        self.assertEqual(set(archives), {2025, 2024, 2022})
        self.assertTrue(archives[2025].endswith("2025-CERTIFIED-EXPORT.zip"))

    def test_raises_when_no_zip_links_found(self):
        with patch(
            "counties.brazos.management.commands.import_brazos_assessment_history.requests.get",
            return_value=self._mock_response("<html><body>nothing here</body></html>"),
        ):
            with self.assertRaises(CommandError):
                Command()._list_archives("https://brazoscad.org/certified-data-downloads/")


class RollUpYearTests(TestCase):
    def _write(self, tmp: str, lines: list[str]) -> Path:
        path = Path(tmp) / ENTITY_INFO_FILENAME
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return path

    def test_rolls_up_the_real_2025_row_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [REAL_2025_ROW])
            rollups = Command()._roll_up_year(path, 2025, accounts=None)

        [rollup] = rollups
        self.assertEqual(rollup.account_number, "000000010013")
        self.assertEqual(rollup.assessed_value, Decimal("242613"))
        self.assertEqual(rollup.appraised_value, Decimal("243298"))
        self.assertEqual(rollup.market_value, Decimal("676378"))
        self.assertEqual(rollup.cap_account, "Y")

    def test_first_entity_row_wins_not_a_pick(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                [
                    _entity_info_line(
                        "000000010013", "02025", "G1", assessed_raw="000000000242613"
                    ),
                    _entity_info_line(
                        "000000010013", "02025", "S1", assessed_raw="000000000999999"
                    ),
                ],
            )
            rollups = Command()._roll_up_year(path, 2025, accounts=None)

        [rollup] = rollups
        self.assertEqual(rollup.assessed_value, Decimal("242613"))

    def test_mismatched_in_record_tax_year_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [_entity_info_line("000000010013", "02024", "G1")])
            rollups = Command()._roll_up_year(path, 2025, accounts=None)

        self.assertEqual(rollups, [])

    def test_scoped_accounts_excludes_unlisted_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                [
                    _entity_info_line("000000010013", "02025", "G1"),
                    _entity_info_line("000000099999", "02025", "G1"),
                ],
            )
            rollups = Command()._roll_up_year(path, 2025, accounts={"000000010013"})

        self.assertEqual([r.account_number for r in rollups], ["000000010013"])


class VerifySaneOrderTests(SimpleTestCase):
    def test_passes_when_order_holds(self):
        rollups = [
            YearRollup("A", Decimal("1"), Decimal("2"), Decimal("3")),
            YearRollup("B", Decimal("1"), Decimal("1"), Decimal("1")),
        ]
        Command()._verify_sane_order(2025, rollups)  # must not raise

    def test_raises_when_most_rows_violate_order(self):
        rollups = [YearRollup(str(i), Decimal("3"), Decimal("2"), Decimal("1")) for i in range(10)]
        with self.assertRaises(CommandError) as ctx:
            Command()._verify_sane_order(2025, rollups)
        self.assertIn("market >= appraised >= assessed", str(ctx.exception))

    def test_raises_when_nothing_is_checkable(self):
        rollups = [YearRollup("A", None, None, None)]
        with self.assertRaises(CommandError):
            Command()._verify_sane_order(2025, rollups)


class ResolveEntityInfoFileTests(SimpleTestCase):
    def test_finds_a_timestamp_prefixed_flat_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / f"2025-07-23_002022_{ENTITY_INFO_FILENAME}").write_text("x")

            found = Command._resolve_entity_info_file(root)

        self.assertEqual(found.name, f"2025-07-23_002022_{ENTITY_INFO_FILENAME}")

    def test_finds_a_file_nested_under_a_nonstandard_subdirectory(self):
        """2022's real zip nests every file under "2022 CERTIFICATION EXPORT/",
        unlike 2025's flat layout -- confirmed by downloading and extracting
        the real 2022 archive during this ticket's research."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "2022 CERTIFICATION EXPORT"
            nested.mkdir()
            (nested / f"2022-08-23_001488_{ENTITY_INFO_FILENAME}").write_text("x")

            found = Command._resolve_entity_info_file(root)

        self.assertEqual(found.name, f"2022-08-23_001488_{ENTITY_INFO_FILENAME}")

    def test_returns_none_when_nothing_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(Command._resolve_entity_info_file(Path(tmp)))


class ExtractFallbackTests(TestCase):
    """BCAD's 2023 export uses DEFLATE64, which zipfile can't decompress."""

    def test_falls_back_to_7z_when_zipfile_cannot_decompress(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.zip"
            archive.write_bytes(b"not a real zip, doesn't matter, we mock ZipFile")
            extract_dir = Path(tmp) / "out"

            with (
                patch(
                    "counties.brazos.portal.zipfile.ZipFile",
                    side_effect=NotImplementedError("That compression method is not supported"),
                ),
                patch(
                    "counties.brazos.management.commands.import_brazos_assessment_history."
                    "Command._extract_with_7z"
                ) as mock_7z,
            ):
                Command()._extract(archive, extract_dir, dry_run=False)

            mock_7z.assert_called_once_with(archive, extract_dir)

    def test_7z_missing_raises_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.zip"
            extract_dir = Path(tmp) / "out"
            with patch("shutil.which", return_value=None):
                with self.assertRaises(CommandError) as ctx:
                    Command._extract_with_7z(archive, extract_dir)
        self.assertIn("Install p7zip-full", str(ctx.exception))

    def test_7z_nonzero_exit_raises_with_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.zip"
            extract_dir = Path(tmp) / "out"
            with (
                patch("shutil.which", return_value="/usr/bin/7z"),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(
                    returncode=2, stderr="ERROR: CRC failed", stdout=""
                )
                with self.assertRaises(CommandError) as ctx:
                    Command._extract_with_7z(archive, extract_dir)
        self.assertIn("CRC failed", str(ctx.exception))


class ImportCommandTests(TestCase):
    """Full command flow with --skip-download --skip-extract against staged files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.download_dir = root / "downloads"
        self.extract_root = root / "extracted"
        self.download_dir.mkdir(parents=True)
        self.extract_root.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)

    def _settings(self):
        return self.settings(
            BCAD_DOWNLOAD_DIR=str(self.download_dir), BCAD_EXTRACT_DIR=str(self.extract_root)
        )

    def _stage_year(self, year: int, lines: list[str]) -> None:
        (self.download_dir / f"bcad_certified_{year}.zip").write_bytes(b"placeholder")
        year_dir = self.extract_root / str(year)
        year_dir.mkdir(parents=True)
        (year_dir / ENTITY_INFO_FILENAME).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

    def test_loads_a_single_staged_year(self):
        self._stage_year(2025, [REAL_2025_ROW])

        with self._settings():
            call_command(
                "import_brazos_assessment_history",
                "--start-year",
                "2025",
                "--end-year",
                "2025",
                "--skip-download",
                "--skip-extract",
            )

        row = AssessmentHistory.objects.get(
            account_number="000000010013", tax_year=2025, county="brazos"
        )
        self.assertEqual(row.assessed_value, Decimal("242613"))
        self.assertEqual(row.appraised_value, Decimal("243298"))
        self.assertEqual(row.market_value, Decimal("676378"))
        self.assertEqual(row.cap_account, "Y")

    def test_loads_multiple_staged_years(self):
        self._stage_year(
            2024,
            [
                _entity_info_line(
                    "000000010013",
                    "02024",
                    "G1",
                    assessed_raw="000000000220000",
                    market_raw="000000000650000",
                    appraised_raw="000000000220000",
                )
            ],
        )
        self._stage_year(2025, [REAL_2025_ROW])

        with self._settings():
            call_command(
                "import_brazos_assessment_history",
                "--start-year",
                "2024",
                "--end-year",
                "2025",
                "--skip-download",
                "--skip-extract",
            )

        years = set(
            AssessmentHistory.objects.filter(
                account_number="000000010013", county="brazos"
            ).values_list("tax_year", flat=True)
        )
        self.assertEqual(years, {2024, 2025})

    def test_scoped_to_property_account_by_default(self):
        self._stage_year(
            2025,
            [
                REAL_2025_ROW,
                _entity_info_line(
                    "000000099999",
                    "02025",
                    "G1",
                    assessed_raw="000000000050000",
                    market_raw="000000000060000",
                    appraised_raw="000000000050000",
                ),
            ],
        )

        with self._settings():
            call_command(
                "import_brazos_assessment_history",
                "--start-year",
                "2025",
                "--end-year",
                "2025",
                "--skip-download",
                "--skip-extract",
            )

        accounts = set(
            AssessmentHistory.objects.filter(county="brazos").values_list(
                "account_number", flat=True
            )
        )
        self.assertEqual(accounts, {"000000010013"})

    def test_all_accounts_flag_keeps_unmatched_properties(self):
        self._stage_year(
            2025,
            [
                REAL_2025_ROW,
                _entity_info_line(
                    "000000099999",
                    "02025",
                    "G1",
                    assessed_raw="000000000050000",
                    market_raw="000000000060000",
                    appraised_raw="000000000050000",
                ),
            ],
        )

        with self._settings():
            call_command(
                "import_brazos_assessment_history",
                "--start-year",
                "2025",
                "--end-year",
                "2025",
                "--skip-download",
                "--skip-extract",
                "--all-accounts",
            )

        accounts = set(
            AssessmentHistory.objects.filter(county="brazos").values_list(
                "account_number", flat=True
            )
        )
        self.assertEqual(accounts, {"000000010013", "000000099999"})

    def test_rerun_replaces_rather_than_duplicates(self):
        self._stage_year(2025, [REAL_2025_ROW])
        with self._settings():
            call_command(
                "import_brazos_assessment_history",
                "--start-year",
                "2025",
                "--end-year",
                "2025",
                "--skip-download",
                "--skip-extract",
            )
            call_command(
                "import_brazos_assessment_history",
                "--start-year",
                "2025",
                "--end-year",
                "2025",
                "--skip-download",
                "--skip-extract",
            )

        self.assertEqual(
            AssessmentHistory.objects.filter(county="brazos", tax_year=2025).count(), 1
        )

    def test_other_countys_rows_and_other_years_survive_a_reload(self):
        AssessmentHistory.objects.create(
            account_number="0000000000001", tax_year=2025, county="harris"
        )
        self._stage_year(2025, [REAL_2025_ROW])
        with self._settings():
            call_command(
                "import_brazos_assessment_history",
                "--start-year",
                "2025",
                "--end-year",
                "2025",
                "--skip-download",
                "--skip-extract",
            )

        self.assertTrue(AssessmentHistory.objects.filter(county="harris", tax_year=2025).exists())

    def test_dry_run_writes_nothing(self):
        self._stage_year(2025, [REAL_2025_ROW])
        with self._settings():
            call_command(
                "import_brazos_assessment_history",
                "--start-year",
                "2025",
                "--end-year",
                "2025",
                "--skip-download",
                "--skip-extract",
                "--dry-run",
            )

        self.assertFalse(AssessmentHistory.objects.filter(county="brazos").exists())

    def test_missing_staged_archive_raises_a_clear_error(self):
        with self._settings():
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "import_brazos_assessment_history",
                    "--start-year",
                    "2025",
                    "--end-year",
                    "2025",
                    "--skip-download",
                    "--skip-extract",
                )
        self.assertIn("archive not found", str(ctx.exception))

    def test_skip_download_without_end_year_raises(self):
        with self._settings():
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "import_brazos_assessment_history",
                    "--skip-download",
                    "--skip-extract",
                )
        self.assertIn("--end-year is required", str(ctx.exception))

    def test_start_year_after_end_year_raises(self):
        with self._settings():
            with self.assertRaises(CommandError):
                call_command(
                    "import_brazos_assessment_history",
                    "--start-year",
                    "2026",
                    "--end-year",
                    "2025",
                    "--skip-download",
                    "--skip-extract",
                )

    def test_bad_layout_year_aborts_without_writing(self):
        """A year whose market/appraised/assessed values violate the expected
        order must not be silently written -- it's the signal that the
        field layout drifted for that year (see ENTITY_INFO_LAYOUT's
        docstring: only 2022 and 2025 are verified)."""
        self._stage_year(
            2025,
            [
                _entity_info_line(
                    f"{i:012d}",
                    "02025",
                    "G1",
                    assessed_raw="000000000300000",
                    appraised_raw="000000000200000",
                    market_raw="000000000100000",  # market < appraised < assessed: backwards
                )
                for i in range(20)
            ],
        )

        with self._settings():
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "import_brazos_assessment_history",
                    "--start-year",
                    "2025",
                    "--end-year",
                    "2025",
                    "--skip-download",
                    "--skip-extract",
                    "--all-accounts",
                )

        self.assertIn("market >= appraised >= assessed", str(ctx.exception))
        self.assertFalse(AssessmentHistory.objects.filter(county="brazos").exists())


class EvaluateCapStatusIntegrationTests(TestCase):
    """The point of this ticket: a real year-over-year cap analysis works
    once multiple years are loaded, with no code changes to
    assessment_history_rows -- it was already county-agnostic.

    evaluate_cap_status itself is intentionally NOT county-agnostic for cap
    *type*: Brazos's cap_account is a derived "some reduction was applied"
    signal, not HCAD's homestead-specific Y/N/Pending flag, so it can only
    report an honest "unknown" cap type here -- see counties/common/cap_status.py.
    """

    def setUp(self):
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)

    def test_prior_year_row_feeds_the_cap_calculation(self):
        from counties.common.history import assessment_history_rows

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            download_dir = root / "downloads"
            extract_root = root / "extracted"
            for year, assessed, market, appraised in [
                (2024, "000000000220000", "000000000650000", "000000000220000"),
                (2025, "000000000242613", "000000000676378", "000000000243298"),
            ]:
                (download_dir).mkdir(parents=True, exist_ok=True)
                (download_dir / f"bcad_certified_{year}.zip").write_bytes(b"x")
                year_dir = extract_root / str(year)
                year_dir.mkdir(parents=True)
                (year_dir / ENTITY_INFO_FILENAME).write_text(
                    _entity_info_line(
                        "000000010013",
                        f"0{year}",
                        "G1",
                        assessed_raw=assessed,
                        market_raw=market,
                        appraised_raw=appraised,
                    )
                    + "\r\n",
                    encoding="utf-8",
                )

            with self.settings(
                BCAD_DOWNLOAD_DIR=str(download_dir), BCAD_EXTRACT_DIR=str(extract_root)
            ):
                call_command(
                    "import_brazos_assessment_history",
                    "--start-year",
                    "2024",
                    "--end-year",
                    "2025",
                    "--skip-download",
                    "--skip-extract",
                )

        rows = assessment_history_rows("000000010013", county="brazos")
        self.assertEqual([r["tax_year"] for r in rows], [2025, 2024])
        self.assertIsNotNone(rows[0]["increase_percent"])
        # cap_type stays "unknown" -- Brazos's derived flag can't tell us
        # whether this was a homestead cap or something else. See
        # counties/common/tests/test_cap_status.py for the dedicated coverage.
        self.assertEqual(rows[0]["cap_status"]["cap_type"], "unknown")
