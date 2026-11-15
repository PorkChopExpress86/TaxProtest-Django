"""Contract tests for the certified Brazos PACS file descriptions."""

from __future__ import annotations

from dataclasses import fields

from django.test import SimpleTestCase

from counties.brazos.cad_refresh import CAD_LOAD_ORDER
from counties.brazos.parsers.pacs import (
    ENTITY_INFO_LAYOUT,
    IMPROVEMENT_DETAIL_ATTR_LAYOUT,
    IMPROVEMENT_DETAIL_LAYOUT,
    IMPROVEMENT_INFO_LAYOUT,
    INFO_LAYOUT,
    LAND_DETAIL_LAYOUT,
    PACS_FILE_DESCRIPTIONS,
    PacsFileDescription,
    parse_entity_info_line,
    parse_improvement_detail_attr_line,
    parse_improvement_detail_line,
    parse_improvement_info_line,
    parse_info_line,
    parse_land_detail_line,
)


class PacsFileDescriptionTests(SimpleTestCase):
    def test_descriptions_equal_expected_file_set_in_declared_order(self):
        self.assertEqual(
            [description.filename for description in PACS_FILE_DESCRIPTIONS],
            [
                "APPRAISAL_INFO.TXT",
                "APPRAISAL_LAND_DETAIL.TXT",
                "APPRAISAL_IMPROVEMENT_INFO.TXT",
                "APPRAISAL_IMPROVEMENT_DETAIL.TXT",
                "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT",
                "APPRAISAL_ENTITY_INFO.TXT",
            ],
        )

    def test_derived_minimum_length_equals_expected_values(self):
        self.assertNotIn("minimum_length", {field.name for field in fields(PacsFileDescription)})
        self.assertEqual(
            [
                (description.filename, description.minimum_length)
                for description in PACS_FILE_DESCRIPTIONS
            ],
            [
                ("APPRAISAL_INFO.TXT", 987),
                ("APPRAISAL_LAND_DETAIL.TXT", 184),
                ("APPRAISAL_IMPROVEMENT_INFO.TXT", 49),
                ("APPRAISAL_IMPROVEMENT_DETAIL.TXT", 622),
                ("APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT", 87),
                ("APPRAISAL_ENTITY_INFO.TXT", 418),
            ],
        )

    def test_required_fields_and_identity_key_stay_separate_and_exact(self):
        self.assertEqual(
            [
                (description.filename, description.required_fields, description.identity_key)
                for description in PACS_FILE_DESCRIPTIONS
            ],
            [
                ("APPRAISAL_INFO.TXT", ("prop_id",), ("prop_id",)),
                ("APPRAISAL_LAND_DETAIL.TXT", ("prop_id",), ("prop_id", "land_seq")),
                (
                    "APPRAISAL_IMPROVEMENT_INFO.TXT",
                    ("prop_id", "imp_id"),
                    ("prop_id", "imp_id"),
                ),
                (
                    "APPRAISAL_IMPROVEMENT_DETAIL.TXT",
                    ("prop_id", "imp_id"),
                    ("prop_id", "imp_id", "detail_seq"),
                ),
                (
                    "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT",
                    ("prop_id", "imp_id"),
                    ("prop_id", "imp_id", "detail_seq", "attribute_type", "attribute_value"),
                ),
                (
                    "APPRAISAL_ENTITY_INFO.TXT",
                    ("prop_id", "tax_unit_code"),
                    ("prop_id", "tax_unit_code"),
                ),
            ],
        )

    def test_descriptions_bind_expected_layouts_and_row_parsers(self):
        self.assertEqual(
            [
                (description.filename, description.layout, description.parse_fn)
                for description in PACS_FILE_DESCRIPTIONS
            ],
            [
                ("APPRAISAL_INFO.TXT", INFO_LAYOUT, parse_info_line),
                ("APPRAISAL_LAND_DETAIL.TXT", LAND_DETAIL_LAYOUT, parse_land_detail_line),
                (
                    "APPRAISAL_IMPROVEMENT_INFO.TXT",
                    IMPROVEMENT_INFO_LAYOUT,
                    parse_improvement_info_line,
                ),
                (
                    "APPRAISAL_IMPROVEMENT_DETAIL.TXT",
                    IMPROVEMENT_DETAIL_LAYOUT,
                    parse_improvement_detail_line,
                ),
                (
                    "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT",
                    IMPROVEMENT_DETAIL_ATTR_LAYOUT,
                    parse_improvement_detail_attr_line,
                ),
                ("APPRAISAL_ENTITY_INFO.TXT", ENTITY_INFO_LAYOUT, parse_entity_info_line),
            ],
        )

    def test_cad_load_order_is_a_permutation_of_descriptions_in_load_order(self):
        self.assertEqual(len(CAD_LOAD_ORDER), len(PACS_FILE_DESCRIPTIONS))
        self.assertEqual(len(set(CAD_LOAD_ORDER)), len(CAD_LOAD_ORDER))
        self.assertEqual(set(CAD_LOAD_ORDER), set(PACS_FILE_DESCRIPTIONS))
        self.assertEqual(
            [description.filename for description in CAD_LOAD_ORDER],
            [
                "APPRAISAL_INFO.TXT",
                "APPRAISAL_LAND_DETAIL.TXT",
                "APPRAISAL_IMPROVEMENT_INFO.TXT",
                "APPRAISAL_IMPROVEMENT_DETAIL.TXT",
                "APPRAISAL_ENTITY_INFO.TXT",
                "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT",
            ],
        )
