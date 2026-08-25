"""Contract tests for Harris import intent and the source catalog."""

from django.test import SimpleTestCase

from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.source_catalog import (
    DEFAULT_HCAD_SOURCE_CATALOG,
    HarrisImportStage,
    HcadSourceId,
)


class HarrisImportPlanTests(SimpleTestCase):
    def test_property_and_building_plan_keeps_both_stages_when_gis_is_skipped(self):
        plan = HarrisImportPlan.from_stage_flags(
            include_property=True,
            include_building=True,
            include_gis=False,
        )

        selected_ids = [
            source.source_id
            for source in plan.select_sources(DEFAULT_HCAD_SOURCE_CATALOG.all_sources())
        ]

        self.assertEqual(
            selected_ids,
            [HcadSourceId.REAL_ACCOUNT_OWNER, HcadSourceId.REAL_BUILDING_LAND],
        )
        self.assertEqual(plan.legacy_scope, "property-and-building")
        self.assertEqual(
            plan.contract_validation_options(),
            {"skip_building_checks": False, "skip_gis_checks": True},
        )

    def test_legacy_scope_adapter_covers_every_stage_combination(self):
        expected_stages = {
            "full": {
                HarrisImportStage.PROPERTY,
                HarrisImportStage.BUILDING,
                HarrisImportStage.GIS,
            },
            "property-only": {HarrisImportStage.PROPERTY},
            "building-only": {HarrisImportStage.BUILDING},
            "gis-only": {HarrisImportStage.GIS},
            "property-and-building": {HarrisImportStage.PROPERTY, HarrisImportStage.BUILDING},
            "property-and-gis": {HarrisImportStage.PROPERTY, HarrisImportStage.GIS},
            "building-and-gis": {HarrisImportStage.BUILDING, HarrisImportStage.GIS},
        }

        for scope, stages in expected_stages.items():
            with self.subTest(scope=scope):
                self.assertEqual(
                    HarrisImportPlan.from_legacy_scope(scope).stages, frozenset(stages)
                )

    def test_empty_plan_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one stage"):
            HarrisImportPlan.from_stage_flags(
                include_property=False,
                include_building=False,
                include_gis=False,
            )


class HcadSourceCatalogTests(SimpleTestCase):
    def test_cama_and_gis_urls_share_one_fallback_policy(self):
        account = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_ACCOUNT_OWNER)
        gis = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.GIS_PARCELS)

        self.assertEqual(
            DEFAULT_HCAD_SOURCE_CATALOG.candidate_urls(account, reference_year=2026),
            [
                "https://download.hcad.org/data/CAMA/2026/Real_acct_owner.zip",
                "https://download.hcad.org/data/CAMA/2025/Real_acct_owner.zip",
            ],
        )
        self.assertEqual(
            DEFAULT_HCAD_SOURCE_CATALOG.candidate_urls(gis, reference_year=2026),
            ["https://download.hcad.org/data/GIS/Parcels.zip"],
        )

    def test_legacy_manifest_is_derived_from_catalog_facts(self):
        archives = DEFAULT_HCAD_SOURCE_CATALOG.legacy_archives()

        self.assertEqual(
            [archive["filename"] for archive in archives],
            [source.filename for source in DEFAULT_HCAD_SOURCE_CATALOG.all_sources()],
        )
        self.assertIn("Real_jur_exempt.zip", [archive["filename"] for archive in archives])

    def test_catalog_returns_independent_source_copies(self):
        first = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_BUILDING_LAND)
        second = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_BUILDING_LAND)

        first.extract_patterns.append("local-only.txt")

        self.assertNotIn("local-only.txt", second.extract_patterns)
