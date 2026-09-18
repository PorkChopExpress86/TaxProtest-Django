"""Identity-based coverage is measured through real Harris candidates."""

import tempfile
from pathlib import Path

from django.db import connection
from django.test import TransactionTestCase

from counties.common.models import ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisPrepare,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
from counties.harris.models import BuildingDetail, PropertyRecord


class HarrisCoverageTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.filter(county="harris"):
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def prepare(self, root, identities, *, room_facts=True, prices=False, plan="full"):
        import geopandas as gpd
        from shapely.geometry import Point

        base = Path(root) / "extracted"
        accounts, buildings, gis = (
            base / "Real_acct_owner",
            base / "Real_building_land",
            base / "Parcels",
        )
        for directory in (accounts, buildings, gis):
            directory.mkdir(parents=True)
        (accounts / "real_acct.txt").write_text(
            "acct\tstate_class\ttot_appr_val\n"
            + "".join(f"{key}\tA1\t{'250000' if prices else ''}\n" for key in identities)
        )
        (buildings / "building_res.txt").write_text(
            "acct\tbld_num\theat_ar\n" + "".join(f"{key}\t1\t1800\n" for key in identities)
        )
        (buildings / "fixtures.txt").write_text(
            "acct\tbld_num\ttype\tunits\n"
            + (
                "".join(f"{key}\t1\tRMB\t3\n{key}\t1\tRMF\t2\n" for key in identities)
                if room_facts
                else ""
            )
        )
        (buildings / "extra_features.txt").write_text(
            "acct\tbld_num\tcd\n" + "".join(f"{key}\t1\tGAR\n" for key in identities)
        )
        gpd.GeoDataFrame(
            {"ACCT": identities},
            geometry=[Point(3100000 + i, 13800000) for i in range(len(identities))],
            crs="EPSG:2278",
        ).to_file(gis / "parcels.shp")
        return run_harris_import(
            HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope(plan),
                data_year=2026,
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisPrepare(validate_completeness=False),
            )
        )

    def baseline(self, count=100):
        for number in range(count):
            key = f"P{number:03d}"
            prop = PropertyRecord.objects.create(
                account_number=key,
                address=key,
                is_residential=True,
                is_data_ready=True,
                latitude=29.7,
                longitude=-95.4,
            )
            BuildingDetail.objects.create(
                property=prop,
                account_number=key,
                building_number=1,
                bedrooms=3,
                bathrooms=2,
                heat_area=1800,
            )

    def test_exact_one_percent_is_allowed_and_growth_cannot_mask_two_percent_loss(self):
        self.baseline()
        for missing in (1, 2):
            with (
                self.subTest(missing=missing),
                tempfile.TemporaryDirectory() as root,
                self.settings(**_runtime_settings(root)),
            ):
                identities = [f"P{number:03d}" for number in range(missing, 100)] + [
                    f"NEW{number}" for number in range(100)
                ]
                result = self.prepare(root, identities)
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                coverage = candidate.evidence["coverage"]
                search = coverage["outcomes"]["search"]
                self.assertEqual(search["prior_ready"], 100)
                self.assertEqual(search["unexplained_loss"], missing)
                self.assertEqual(search["retained_ready"], 100 - missing)
                self.assertEqual(search["additions"], 100)
                self.assertEqual(search["verified_removals"], [])
                self.assertEqual(coverage["requires_review"], missing == 2)
                self.assertEqual(PropertyRecord.objects.count(), 100)

    def test_first_import_requires_review_with_unavailable_comparison(self):
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            result = self.prepare(root, ["P100"])
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertTrue(candidate.evidence["coverage"]["requires_review"])
            self.assertEqual(candidate.state, "awaiting_review")
            self.assertIsNone(candidate.evidence["coverage"]["outcomes"]["search"]["loss_fraction"])

    def test_zero_ready_with_valid_sources_is_a_hard_qualification_failure(self):
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            result = self.prepare(root, ["P100"], room_facts=False)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.state, "blocked")
            self.assertTrue(candidate.evidence["coverage"]["hard_failures"])
            self.assertTrue(candidate.evidence["result"]["stages"]["source_validation"]["success"])
            self.assertIn(
                "Active bedroom/bathroom facts unavailable",
                str(candidate.evidence["coverage"]["outcomes"]["search"]["exclusion_reasons"]),
            )
            self.assertFalse(PropertyRecord.objects.exists())

    def test_ineligible_neighbors_cannot_qualify_an_equity_report(self):
        self.baseline(4)
        PropertyRecord.objects.update(value=250000)
        PropertyRecord.objects.exclude(account_number="P000").update(
            is_residential=False, is_data_ready=False
        )
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            result = self.prepare(root, [f"P{number:03d}" for number in range(4)], plan="gis-only")
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            report = candidate.evidence["coverage"]["outcomes"]["report"]
            self.assertEqual(report["total_ready"], 0)
            self.assertTrue(report["supported"])
            self.assertIn("three qualifying", str(report["exclusion_reasons"]))

    def test_unavailable_new_year_tax_inputs_do_not_block_property_coverage(self):
        self.baseline(4)
        PropertyRecord.objects.update(value=250000)
        ImportOperation.objects.create(
            county="harris",
            intent="full",
            status="published",
            requested_year=2025,
            publication_after={"property_source_year": 2025},
        )
        TaxUnitRate.objects.create(
            county="harris", tax_year=2025, tax_unit_code="C", adopted_rate="0.01"
        )
        for number in range(4):
            PropertyJurisdictionExemption.objects.create(
                county="harris",
                tax_year=2025,
                account_number=f"P{number:03d}",
                tax_unit_code="C",
                taxable_value=250000,
            )
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            result = self.prepare(root, [f"P{number:03d}" for number in range(4)], prices=True)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            coverage = candidate.evidence["coverage"]
            self.assertEqual(coverage["outcomes"]["tax"]["prior_ready"], 4)
            self.assertFalse(coverage["outcomes"]["tax"]["supported"])
            self.assertEqual(coverage["outcomes"]["tax"]["total_ready"], 0)
            self.assertTrue(coverage["automatic_publication_allowed"])
            self.assertEqual(
                PropertyJurisdictionExemption.objects.filter(county="harris").count(), 4
            )
