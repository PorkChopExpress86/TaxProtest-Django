"""Tests for the Brazos CAD fixed-width layout definitions.

The sample records here are verbatim prefixes of real rows from the 2025
certified export, so a regression in the byte offsets shows up as a wrong value
rather than a silent shift.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from data.brazos_layouts import (
    APPRAISAL_ENTITY,
    APPRAISAL_IMPROVEMENT_DETAIL,
    APPRAISAL_IMPROVEMENT_INFO,
    APPRAISAL_INFO,
    APPRAISAL_LAND_DETAIL,
    BOOL,
    DECIMAL,
    INT,
    LAYOUTS,
    TEXT,
    Field,
    _convert,
    parse_header,
    parse_record,
)


class FieldConversionTests(SimpleTestCase):
    def test_text_keeps_blank_as_empty_string_not_null(self):
        # Target columns use Django's blank=True (NOT NULL) convention, so blank
        # text must not become SQL NULL or the COPY would fail.
        field = Field("situs_city", 1, 10, TEXT)
        self.assertEqual(_convert(field, "          "), "")
        self.assertEqual(_convert(field, "BRYAN     "), "BRYAN")

    def test_int_strips_zero_padding(self):
        field = Field("prop_id", 1, 12, INT)
        self.assertEqual(_convert(field, "000000010002"), "10002")

    def test_int_blank_becomes_null(self):
        field = Field("sup_num", 1, 12, INT)
        self.assertIsNone(_convert(field, "            "))

    def test_int_garbage_becomes_null_rather_than_raising(self):
        # One malformed row must not abort a multi-gigabyte load.
        field = Field("prop_id", 1, 12, INT)
        self.assertIsNone(_convert(field, "ABC/*&^%   "))

    def test_decimal_with_implied_scale(self):
        # size_acres: numeric(14) carrying 4 implied decimals, no decimal point.
        field = Field("size_acres", 1, 14, DECIMAL, scale=4)
        self.assertEqual(_convert(field, "00000005739000"), "573.9000")

    def test_decimal_with_explicit_point_ignores_scale(self):
        # imprv_val ships its own decimal point; the implied scale must not
        # be applied a second time.
        field = Field("imprv_val", 1, 14, DECIMAL, scale=4)
        self.assertEqual(_convert(field, "0006193.000000"), "6193.000000")

    def test_decimal_without_scale_is_whole_units(self):
        field = Field("assessed_val", 1, 15, DECIMAL)
        self.assertEqual(_convert(field, "000000002865095"), "2865095")

    def test_decimal_handles_blank_padding(self):
        # ag_value is blank-padded rather than zero-padded in the real export.
        field = Field("ag_value", 1, 14, DECIMAL)
        self.assertEqual(_convert(field, "        105024"), "105024")

    def test_bool_tokens(self):
        field = Field("ag_apply", 1, 1, BOOL)
        self.assertEqual(_convert(field, "T"), "t")
        self.assertEqual(_convert(field, "Y"), "t")
        self.assertEqual(_convert(field, "F"), "f")
        self.assertEqual(_convert(field, "N"), "f")
        self.assertIsNone(_convert(field, " "))
        self.assertIsNone(_convert(field, "?"))


class LayoutIntegrityTests(SimpleTestCase):
    def test_all_layouts_have_unique_column_names(self):
        for layout in LAYOUTS:
            names = layout.column_names
            self.assertEqual(
                len(names), len(set(names)), f"{layout.model} has duplicate column names"
            )

    def test_conflict_columns_are_real_columns(self):
        for layout in LAYOUTS:
            for column in layout.conflict_columns:
                self.assertIn(
                    column,
                    layout.column_names,
                    f"{layout.model} conflict column {column!r} is not a mapped field",
                )

    def test_field_ranges_are_well_formed(self):
        for layout in LAYOUTS:
            for field in layout.fields:
                self.assertGreaterEqual(field.start, 1, f"{layout.model}.{field.name}")
                self.assertGreaterEqual(
                    field.end, field.start, f"{layout.model}.{field.name} ends before it starts"
                )

    def test_fields_do_not_overlap(self):
        for layout in LAYOUTS:
            occupied: dict[int, str] = {}
            for field in layout.fields:
                for position in range(field.start, field.end + 1):
                    clash = occupied.get(position)
                    self.assertIsNone(
                        clash,
                        f"{layout.model}: {field.name} overlaps {clash} at position {position}",
                    )
                    occupied[position] = field.name

    def test_min_width_matches_last_mapped_field(self):
        self.assertEqual(APPRAISAL_ENTITY.min_width, 17)
        self.assertEqual(APPRAISAL_IMPROVEMENT_INFO.min_width, 114)
        self.assertEqual(APPRAISAL_LAND_DETAIL.min_width, 199)
        self.assertEqual(APPRAISAL_IMPROVEMENT_DETAIL.min_width, 622)

    def test_optional_fields_are_excluded_from_min_width(self):
        # circuit_breaker_val sits at 9068-9082, past the published 9067-char
        # layout. Counting it would reject any export that stops at the
        # documented width.
        self.assertLessEqual(APPRAISAL_INFO.min_width, 9067)
        optional = [f for f in APPRAISAL_INFO.fields if f.optional]
        self.assertEqual([f.name for f in optional], ["circuit_breaker_val"])
        self.assertGreater(max(f.end for f in optional), APPRAISAL_INFO.min_width)

    def test_situs_number_is_mapped(self):
        # The street number lives at 4460, far from the rest of the address.
        names = APPRAISAL_INFO.column_names
        self.assertIn("situs_num", names)
        self.assertIn("situs_unit", names)

    def test_post_productivity_loss_values_are_mapped(self):
        names = APPRAISAL_INFO.column_names
        self.assertIn("appraised_val_prod_loss", names)
        self.assertIn("assessed_val_prod_loss", names)


class HeaderTests(SimpleTestCase):
    """The header record pins down which export a load came from."""

    # Verbatim APPRAISAL_HEADER.TXT from the 2025 certified export.
    REAL = (
        "07/23/2025 07:162025 CERTIFIED ROLL EXPORT 7.23.25      20250000<ALL>"
        "                                             BRAZOS CENTRAL APPRAISAL DISTR"
        "SONDRA              8.50.2.11 8.0.0.30            As of Supplement"
    )

    def test_parses_real_header(self):
        header = parse_header(self.REAL)
        self.assertEqual(header["run_date"], "07/23/2025 07:16")
        self.assertEqual(header["file_description"], "2025 CERTIFIED ROLL EXPORT 7.23.25")
        self.assertEqual(header["appraisal_year"], "2025")
        self.assertEqual(header["supplement_num"], "0")
        self.assertEqual(header["entity_cd"], "<ALL>")
        self.assertEqual(header["office_name"], "BRAZOS CENTRAL APPRAISAL DISTR")
        self.assertEqual(header["pacs_version"], "8.50.2.11")

    def test_short_header_does_not_raise(self):
        header = parse_header("07/23/2025 07:16")
        self.assertEqual(header["run_date"], "07/23/2025 07:16")
        self.assertIsNone(header["appraisal_year"])


class RealRecordTests(SimpleTestCase):
    """Parse verbatim rows from the 2025 certified export."""

    def test_parses_improvement_record(self):
        line = (
            "0000000100022025000000100000R         RESIDENTIAL              "
            "D2   N0006193.00000000000100.000000N000000000000000"
        )
        self.assertEqual(len(line), 114)
        values = dict(
            zip(
                APPRAISAL_IMPROVEMENT_INFO.column_names,
                parse_record(APPRAISAL_IMPROVEMENT_INFO, line),
            )
        )
        self.assertEqual(values["prop_id"], "10002")
        self.assertEqual(values["tax_year"], "2025")
        self.assertEqual(values["imp_id"], "100000")
        self.assertEqual(values["imprv_type_cd"], "R")
        self.assertEqual(values["imprv_type_desc"], "RESIDENTIAL")
        self.assertEqual(values["imprv_state_cd"], "D2")
        self.assertEqual(values["imprv_homesite"], "f")
        self.assertEqual(values["imprv_val"], "6193.000000")
        self.assertEqual(values["omitted"], "f")

    def test_parses_land_record(self):
        line = (
            "0000000100022025000000100000A1        IMPROVED PASTURE         "
            "D1   F00000005739000000000249990840000000000000000000000000000A    "
            "BO11RAZ3  00000002833677TA    A1                105024            100"
        )
        self.assertEqual(len(line), 199)
        values = dict(
            zip(APPRAISAL_LAND_DETAIL.column_names, parse_record(APPRAISAL_LAND_DETAIL, line))
        )
        self.assertEqual(values["prop_id"], "10002")
        self.assertEqual(values["land_seg_id"], "100000")
        self.assertEqual(values["land_type_cd"], "A1")
        self.assertEqual(values["land_type_desc"], "IMPROVED PASTURE")
        self.assertEqual(values["state_cd"], "D1")
        self.assertEqual(values["land_seg_homesite"], "f")
        # 4 implied decimals
        self.assertEqual(values["size_acres"], "573.9000")
        # no implied decimals
        self.assertEqual(values["size_square_feet"], "24999084")
        self.assertEqual(values["mkt_ls_class"], "BO11RAZ3")
        self.assertEqual(values["land_seg_mkt_val"], "2833677")
        self.assertEqual(values["ag_apply"], "t")
        # Blank-padded rather than zero-padded in the real export.
        self.assertEqual(values["ag_value"], "105024")
        self.assertEqual(values["land_homesite_pct"], "100")

    def test_parses_entity_record(self):
        values = dict(
            zip(APPRAISAL_ENTITY.column_names, parse_record(APPRAISAL_ENTITY, "000000237982C1   "))
        )
        self.assertEqual(values["entity_id"], "237982")
        self.assertEqual(values["entity_cd"], "C1")
