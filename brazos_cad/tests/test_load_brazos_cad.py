"""Tests for the fixed-width BCAD loader (brazos_cad management command).

Replaces the old COPY-based test_copy_ingest.py: the loader now parses real
fixed-width records (see brazos_cad/parsers/pacs.py) and loads them via the
Django ORM, so plain TestCase is sufficient — no more raw-cursor TRUNCATE
workaround needed for a psycopg2 COPY path that no longer exists.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from brazos_cad.management.commands.load_brazos_cad import (
    ENTITY_INFO_FILENAME,
    IMPROVEMENT_DETAIL_ATTR_FILENAME,
    IMPROVEMENT_DETAIL_FILENAME,
    INGEST_SPECS,
    Command,
    _parse_bathrooms,
)
from brazos_cad.models import (
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)
from data.models import PropertyJurisdictionExemption


def _line(length: int, fields: dict[tuple[int, int], str]) -> str:
    chars = [" "] * length
    for (start, end), value in fields.items():
        value = value[: end - start]
        chars[start : start + len(value)] = list(value)
    return "".join(chars)


def _land_detail_line(
    prop_id: str, tax_year: str, land_seq: str, land_value_raw: str, acreage_raw: str
) -> str:
    return _line(
        199,
        {
            (0, 12): prop_id,
            (12, 16): tax_year,
            (16, 28): land_seq,
            (28, 32): "A1",
            (38, 63): "IMPROVED PASTURE",
            (140, 154): land_value_raw,
            (178, 184): acreage_raw,
        },
    )


def _improvement_info_line(prop_id: str, tax_year: str, imp_id: str) -> str:
    return _line(
        114,
        {
            (0, 12): prop_id,
            (12, 16): tax_year,
            (16, 28): imp_id,
            (28, 31): "R",
            (38, 49): "RESIDENTIAL",
        },
    )


def _improvement_detail_line(
    prop_id: str,
    tax_year: str,
    imp_id: str,
    detail_seq: str,
    year_built: str,
    detail_value_raw: str,
) -> str:
    return _line(
        622,
        {
            (0, 12): prop_id,
            (12, 16): tax_year,
            (16, 28): imp_id,
            (28, 40): detail_seq,
            (50, 75): "MAIN AREA",
            (85, 89): year_built,
            (93, 108): "00001000.000000",
            (108, 122): detail_value_raw,
            (122, 622): "R30,U60,L30,D60",
        },
    )


def _entity_info_line(
    prop_id: str,
    tax_year: str,
    tax_unit_code: str,
    assessed_value_raw: str,
    taxable_value_raw: str,
    *,
    hs_amt_raw: str = "000000000000000",
    ov65_amt_raw: str = "000000000000000",
    dp_amt_raw: str = "000000000000000",
) -> str:
    return _line(
        2750,
        {
            (0, 12): prop_id,
            (12, 17): tax_year,
            (41, 53): "000000237993",
            (53, 63): tax_unit_code,
            (63, 113): "BRAZOS COUNTY",
            (148, 163): assessed_value_raw,
            (163, 178): taxable_value_raw,
            (298, 313): hs_amt_raw,
            (313, 328): ov65_amt_raw,
            (328, 343): dp_amt_raw,
        },
    )


class ResolveTextFilesTests(TestCase):
    def test_matches_timestamp_prefixed_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for spec in INGEST_SPECS:
                (root / f"2025-07-23_002022_{spec.filename}").write_text("x", encoding="utf-8")
            (root / f"2025-07-23_002022_{IMPROVEMENT_DETAIL_FILENAME}").write_text(
                "x", encoding="utf-8"
            )

            resolved = Command._resolve_text_files(root)

            for spec in INGEST_SPECS:
                self.assertIn(spec.filename, resolved)
                self.assertEqual(resolved[spec.filename].name, f"2025-07-23_002022_{spec.filename}")
            self.assertIn(IMPROVEMENT_DETAIL_FILENAME, resolved)

    def test_picks_newest_when_duplicate_timestamped_copies_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            filename = "APPRAISAL_LAND_DETAIL.TXT"
            (root / f"2024-01-01_000000_{filename}").write_text("stale", encoding="utf-8")
            (root / f"2025-07-23_002022_{filename}").write_text("fresh", encoding="utf-8")

            resolved = Command._resolve_text_files(root)

            self.assertEqual(resolved[filename].name, f"2025-07-23_002022_{filename}")


class LoadFileTests(TestCase):
    def test_land_detail_loads_with_correct_decimal_scaling(self):
        spec = next(s for s in INGEST_SPECS if s.filename == "APPRAISAL_LAND_DETAIL.TXT")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / spec.filename
            path.write_text(
                _land_detail_line(
                    "000000010002", "2025", "000000100000", "00000002833677", "105024"
                )
                + "\r\n",
                encoding="utf-8",
            )

            count = Command()._load_file(spec, path, 2025, dry_run=False)

        self.assertEqual(count, 1)
        row = PropertyLand.objects.get(prop_id="000000010002", tax_year=2025)
        self.assertEqual(row.land_value, Decimal("28336.77"))
        self.assertEqual(row.acreage, Decimal("10.5024"))

    def test_reloading_same_tax_year_replaces_rather_than_duplicates(self):
        spec = next(s for s in INGEST_SPECS if s.filename == "APPRAISAL_LAND_DETAIL.TXT")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / spec.filename
            path.write_text(
                _land_detail_line(
                    "000000010002", "2025", "000000100000", "00000002833677", "105024"
                )
                + "\r\n",
                encoding="utf-8",
            )
            Command()._load_file(spec, path, 2025, dry_run=False)
            Command()._load_file(spec, path, 2025, dry_run=False)

        self.assertEqual(PropertyLand.objects.filter(tax_year=2025).count(), 1)

    def test_loading_one_year_does_not_delete_another_years_rows(self):
        spec = next(s for s in INGEST_SPECS if s.filename == "APPRAISAL_LAND_DETAIL.TXT")
        PropertyLand.objects.create(prop_id="000000099999", tax_year=2024, land_seq=1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / spec.filename
            path.write_text(
                _land_detail_line(
                    "000000010002", "2025", "000000100000", "00000002833677", "105024"
                )
                + "\r\n",
                encoding="utf-8",
            )
            Command()._load_file(spec, path, 2025, dry_run=False)

        self.assertTrue(PropertyLand.objects.filter(tax_year=2024).exists())
        self.assertTrue(PropertyLand.objects.filter(tax_year=2025).exists())

    def test_row_with_mismatched_in_record_tax_year_is_skipped(self):
        spec = next(s for s in INGEST_SPECS if s.filename == "APPRAISAL_LAND_DETAIL.TXT")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / spec.filename
            # In-record tax_year is 2024, but we request 2025.
            path.write_text(
                _land_detail_line(
                    "000000010002", "2024", "000000100000", "00000002833677", "105024"
                )
                + "\r\n",
                encoding="utf-8",
            )
            count = Command()._load_file(spec, path, 2025, dry_run=False)

        self.assertEqual(count, 0)
        self.assertFalse(PropertyLand.objects.filter(tax_year=2025).exists())


class LoadImprovementDetailRollupTests(TestCase):
    def test_year_built_rolls_up_from_highest_value_detail_row(self):
        improvement_spec = next(
            s for s in INGEST_SPECS if s.filename == "APPRAISAL_IMPROVEMENT_INFO.TXT"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            info_path = root / improvement_spec.filename
            info_path.write_text(
                _improvement_info_line("000000010002", "2025", "000000100000") + "\r\n",
                encoding="utf-8",
            )
            Command()._load_file(improvement_spec, info_path, 2025, dry_run=False)

            detail_path = root / IMPROVEMENT_DETAIL_FILENAME
            lines = [
                # Smaller detail_value / older year_built.
                _improvement_detail_line(
                    "000000010002", "2025", "000000100000", "000000000001", "1980", "0001000.000000"
                ),
                # Larger detail_value / newer year_built — should win the rollup.
                _improvement_detail_line(
                    "000000010002", "2025", "000000100000", "000000000002", "1998", "0005000.000000"
                ),
            ]
            detail_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

            count = Command()._load_improvement_detail(detail_path, 2025, dry_run=False)

        self.assertEqual(count, 2)
        self.assertEqual(PropertyImprovementDetail.objects.filter(tax_year=2025).count(), 2)
        improvement = PropertyImprovement.objects.get(imp_id="000000100000", tax_year=2025)
        self.assertEqual(improvement.year_built, 1998)

    def test_zero_year_built_rows_excluded_from_rollup(self):
        improvement_spec = next(
            s for s in INGEST_SPECS if s.filename == "APPRAISAL_IMPROVEMENT_INFO.TXT"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info_path = root / improvement_spec.filename
            info_path.write_text(
                _improvement_info_line("000000010002", "2025", "000000100000") + "\r\n",
                encoding="utf-8",
            )
            Command()._load_file(improvement_spec, info_path, 2025, dry_run=False)

            detail_path = root / IMPROVEMENT_DETAIL_FILENAME
            detail_path.write_text(
                _improvement_detail_line(
                    "000000010002", "2025", "000000100000", "000000000001", "0000", "0005000.000000"
                )
                + "\r\n",
                encoding="utf-8",
            )
            Command()._load_improvement_detail(detail_path, 2025, dry_run=False)

        improvement = PropertyImprovement.objects.get(imp_id="000000100000", tax_year=2025)
        self.assertIsNone(improvement.year_built)


class LoadEntityInfoTests(TestCase):
    def test_creates_base_row_plus_one_row_per_nonzero_exemption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ENTITY_INFO_FILENAME
            path.write_text(
                _entity_info_line(
                    "000000010013",
                    "02025",
                    "G1",
                    "000000000242613",
                    "000000000167613",
                    ov65_amt_raw="000000000075000",
                )
                + "\r\n",
                encoding="utf-8",
            )

            count = Command()._load_entity_info(path, 2025, dry_run=False)

        self.assertEqual(count, 2)
        rows = PropertyJurisdictionExemption.objects.filter(
            account_number="000000010013", tax_year=2025, county="brazos"
        )
        self.assertEqual(rows.count(), 2)

        base = rows.get(exemption_code="")
        self.assertEqual(base.tax_unit_code, "G1")
        self.assertEqual(base.assessed_value, Decimal("242613"))
        self.assertEqual(base.taxable_value, Decimal("167613"))
        self.assertIsNone(base.exemption_amount)

        ov65 = rows.get(exemption_code="OV65")
        self.assertEqual(ov65.exemption_amount, Decimal("75000"))
        self.assertEqual(ov65.taxable_value, Decimal("167613"))

    def test_rolls_up_assessed_value_onto_property_account(self):
        """Regression guard for the GIS-shapefile year-mislabeling bug: this
        is the ONLY reliable, tax_year-accurate source for
        PropertyAccount.assessed_value -- load_brazos_gis must never
        overwrite it (see test_load_brazos_gis.py's matching guard)."""
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ENTITY_INFO_FILENAME
            path.write_text(
                _entity_info_line(
                    "000000010013",
                    "02025",
                    "G1",
                    "000000000242613",
                    "000000000167613",
                    ov65_amt_raw="000000000075000",
                )
                + "\r\n",
                encoding="utf-8",
            )

            Command()._load_entity_info(path, 2025, dry_run=False)

        account = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertEqual(account.assessed_value, Decimal("242613"))

    def test_assessed_value_rollup_takes_first_entity_not_a_pick(self):
        """assessed_value is confirmed consistent across every entity for a
        given property (unlike taxable_value, which varies by exemption) --
        the first non-null value encountered per prop_id is authoritative,
        not a tiebreak among differing candidates."""
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ENTITY_INFO_FILENAME
            lines = [
                _entity_info_line(
                    "000000010013", "02025", "G1", "000000000242613", "000000000167613"
                ),
                _entity_info_line(
                    "000000010013", "02025", "S1", "000000000242613", "000000000139667"
                ),
            ]
            path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

            Command()._load_entity_info(path, 2025, dry_run=False)

        account = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertEqual(account.assessed_value, Decimal("242613"))

    def test_all_zero_amounts_create_only_the_base_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ENTITY_INFO_FILENAME
            path.write_text(
                _entity_info_line(
                    "000000010055", "02025", "S1", "000000000100000", "000000000100000"
                )
                + "\r\n",
                encoding="utf-8",
            )

            count = Command()._load_entity_info(path, 2025, dry_run=False)

        self.assertEqual(count, 1)
        row = PropertyJurisdictionExemption.objects.get(
            account_number="000000010055", tax_year=2025, county="brazos"
        )
        self.assertEqual(row.exemption_code, "")

    def test_reload_scoped_to_brazos_leaves_harris_rows_untouched(self):
        PropertyJurisdictionExemption.objects.create(
            account_number="9999990000001",
            tax_year=2025,
            county="harris",
            tax_unit_code="U1",
            exemption_code="HS",
            exemption_amount=Decimal("40000"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ENTITY_INFO_FILENAME
            path.write_text(
                _entity_info_line(
                    "000000010055", "02025", "S1", "000000000100000", "000000000100000"
                )
                + "\r\n",
                encoding="utf-8",
            )
            Command()._load_entity_info(path, 2025, dry_run=False)
            Command()._load_entity_info(path, 2025, dry_run=False)

        self.assertTrue(
            PropertyJurisdictionExemption.objects.filter(
                account_number="9999990000001", county="harris"
            ).exists()
        )
        self.assertEqual(
            PropertyJurisdictionExemption.objects.filter(
                account_number="000000010055", county="brazos", tax_year=2025
            ).count(),
            1,
        )

    def test_row_with_mismatched_in_record_tax_year_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ENTITY_INFO_FILENAME
            path.write_text(
                _entity_info_line(
                    "000000010055", "02024", "S1", "000000000100000", "000000000100000"
                )
                + "\r\n",
                encoding="utf-8",
            )

            count = Command()._load_entity_info(path, 2025, dry_run=False)

        self.assertEqual(count, 0)
        self.assertFalse(
            PropertyJurisdictionExemption.objects.filter(tax_year=2025, county="brazos").exists()
        )


def _attr_line(
    prop_id: str,
    tax_year: str,
    imp_id: str,
    detail_seq: str,
    attribute_type: str,
    attribute_value: str,
) -> str:
    return _line(
        87,
        {
            (0, 12): prop_id,
            (12, 16): tax_year,
            (16, 28): imp_id,
            (28, 40): detail_seq,
            (52, 77): attribute_type,
            (77, 87): attribute_value,
        },
    )


class ParseBathroomsTests(TestCase):
    """Direct unit tests for the free-text "Plumbing" parser -- these formats
    are all real values sampled from the 2025 export (see
    WIDE_ATTRIBUTE_FIELDS's module comment for the coverage stats)."""

    def test_plain_integer(self):
        self.assertEqual(_parse_bathrooms("2"), (Decimal("2"), 0))

    def test_slash_format_is_full_slash_half(self):
        self.assertEqual(_parse_bathrooms("3/1"), (Decimal("3"), 1))

    def test_decimal_half_format(self):
        self.assertEqual(_parse_bathrooms("2.5"), (Decimal("2"), 1))

    def test_space_fraction_format(self):
        self.assertEqual(_parse_bathrooms("2 1/2"), (Decimal("2"), 1))

    def test_dash_fraction_format(self):
        self.assertEqual(_parse_bathrooms("3-1/2"), (Decimal("3"), 1))

    def test_comma_fraction_format(self):
        self.assertEqual(_parse_bathrooms("2,1/2"), (Decimal("2"), 1))

    def test_fraction_with_trailing_ea(self):
        self.assertEqual(_parse_bathrooms("1 1/2 EA"), (Decimal("1"), 1))

    def test_backslash_slash_variant(self):
        self.assertEqual(_parse_bathrooms("2\\1"), (Decimal("2"), 1))

    def test_unparseable_value_returns_none_none(self):
        self.assertEqual(_parse_bathrooms("1 3/4"), (None, None))


class LoadImprovementDetailAttrTests(TestCase):
    def test_wide_attributes_aggregate_onto_one_row_per_improvement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / IMPROVEMENT_DETAIL_ATTR_FILENAME
            lines = [
                _attr_line(
                    "000000010008", "2025", "000000100002", "000000100001", "Plumbing", "2/1"
                ),
                _attr_line(
                    "000000010008",
                    "2025",
                    "000000100002",
                    "000000100001",
                    "Number of Bedrooms",
                    "3",
                ),
                _attr_line(
                    "000000010008", "2025", "000000100002", "000000100001", "Exterior Wall", "BV"
                ),
            ]
            path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

            count = Command()._load_improvement_detail_attr(path, 2025, dry_run=False)

        self.assertEqual(count, 1)
        row = PropertyBuildingCharacteristic.objects.get(
            prop_id="000000010008", imp_id="000000100002", tax_year=2025
        )
        self.assertEqual(row.bathrooms, Decimal("2"))
        self.assertEqual(row.half_baths, 1)
        self.assertEqual(row.bedrooms, 3)
        self.assertEqual(row.exterior_wall, "BV")

    def test_first_occurrence_wins_on_duplicate_attribute_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / IMPROVEMENT_DETAIL_ATTR_FILENAME
            lines = [
                _attr_line(
                    "000000010085", "2025", "000001165787", "000000100001", "Foundation", "CS"
                ),
                _attr_line(
                    "000000010085", "2025", "000001165787", "000000100002", "Foundation", "BK"
                ),
            ]
            path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

            Command()._load_improvement_detail_attr(path, 2025, dry_run=False)

        row = PropertyBuildingCharacteristic.objects.get(
            prop_id="000000010085", imp_id="000001165787", tax_year=2025
        )
        self.assertEqual(row.foundation, "CS")

    def test_unrecognized_attribute_types_become_extra_feature_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / IMPROVEMENT_DETAIL_ATTR_FILENAME
            lines = [
                _attr_line(
                    "000000010008", "2025", "000000100002", "000000100001", "Fireplace", "G"
                ),
                # attribute_value is genuinely only 10 chars wide (77:87) on
                # the real export -- "Other Feature" free text is truncated
                # there (real sampled values: "STORAGE BU", "COVERED PA",
                # "WOOD DECK "), not a parsing bug.
                _attr_line(
                    "000000010041",
                    "2025",
                    "000000100037",
                    "000000100047",
                    "Other Feature",
                    "STORAGE BU",
                ),
                _attr_line(
                    "000000010041", "2025", "000000100037", "000000100047", "Other Feature", "SHED"
                ),
            ]
            path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

            Command()._load_improvement_detail_attr(path, 2025, dry_run=False)

        self.assertEqual(
            PropertyExtraFeature.objects.filter(
                prop_id="000000010008", feature_type="Fireplace"
            ).count(),
            1,
        )
        # Both "Other Feature" rows survive as independent entries -- this is
        # the regression this split model design exists to allow, unlike the
        # wide model's first-occurrence-wins collision handling.
        other_features = PropertyExtraFeature.objects.filter(
            prop_id="000000010041", feature_type="Other Feature"
        ).values_list("feature_value", flat=True)
        self.assertCountEqual(other_features, ["STORAGE BU", "SHED"])

    def test_unparseable_plumbing_leaves_bathrooms_null_not_dropped_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / IMPROVEMENT_DETAIL_ATTR_FILENAME
            lines = [
                _attr_line(
                    "000000010008", "2025", "000000100002", "000000100001", "Plumbing", "1 3/4"
                ),
                _attr_line(
                    "000000010008",
                    "2025",
                    "000000100002",
                    "000000100001",
                    "Number of Bedrooms",
                    "3",
                ),
            ]
            path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

            Command()._load_improvement_detail_attr(path, 2025, dry_run=False)

        row = PropertyBuildingCharacteristic.objects.get(
            prop_id="000000010008", imp_id="000000100002", tax_year=2025
        )
        self.assertIsNone(row.bathrooms)
        self.assertEqual(row.bedrooms, 3)

    def test_reload_replaces_rather_than_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / IMPROVEMENT_DETAIL_ATTR_FILENAME
            path.write_text(
                _attr_line("000000010008", "2025", "000000100002", "000000100001", "Fireplace", "G")
                + "\r\n",
                encoding="utf-8",
            )
            Command()._load_improvement_detail_attr(path, 2025, dry_run=False)
            Command()._load_improvement_detail_attr(path, 2025, dry_run=False)

        self.assertEqual(PropertyExtraFeature.objects.filter(tax_year=2025).count(), 1)
