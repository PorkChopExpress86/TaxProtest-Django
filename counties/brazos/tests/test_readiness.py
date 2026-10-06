"""Contract tests for the Brazos active-snapshot/readiness read module."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from counties.brazos.adapter import adapter
from counties.brazos.models import (
    BrazosPropertySnapshot,
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyLand,
    SnapshotOutcome,
)
from counties.brazos.readiness import BrazosActiveSnapshotReadiness
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate
from counties.common.tests.similarity_scenarios import build_brazos_residential_scenario

TARGET = "000000010013"


class ActiveSnapshotReadTests(TestCase):
    def test_projection_uses_the_published_snapshot_not_the_latest_account_row(self):
        prior = BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
            is_active=False,
        )
        active = BrazosPropertySnapshot.objects.create(
            tax_year=2024,
            outcome=SnapshotOutcome.PARTIAL,
            cad_source_year=2024,
        )
        PropertyAccount.objects.create(
            prop_id="000000010013", tax_year=prior.tax_year, owner_name="Newer row"
        )
        PropertyAccount.objects.create(
            prop_id="000000010013", tax_year=active.tax_year, owner_name="Published row"
        )

        projection = BrazosActiveSnapshotReadiness().project("000000010013")

        self.assertEqual(projection.tax_year, 2024)
        self.assertEqual(projection.owner_name, "Published row")
        self.assertTrue(projection.search_ready)

    def test_search_and_projection_share_trimmed_readiness_and_coordinate_provenance(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.PARTIAL,
            cad_source_year=2025,
        )
        PropertyAccount.objects.create(
            prop_id="whitespace-only",
            tax_year=2025,
            owner_name="  ",
            situs_address=" ",
            mailing_address=" ",
        )
        PropertyAccount.objects.create(
            prop_id="source-backed",
            tax_year=2025,
            owner_name="Owner",
            coordinate_source="bcad-certified-gis",
            coordinate_source_year=2025,
        )

        readiness = BrazosActiveSnapshotReadiness()

        self.assertFalse(readiness.project_static("whitespace-only").search_ready)
        self.assertEqual(
            list(readiness.search_queryset().values_list("prop_id", flat=True)), ["source-backed"]
        )
        projection = readiness.project_static("source-backed")
        self.assertEqual(projection.coordinate_source, "bcad-certified-gis")
        self.assertEqual(projection.coordinate_source_year, 2025)

    def test_projection_uses_one_captured_active_snapshot(self):
        snapshot = BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.PARTIAL,
            cad_source_year=2025,
        )
        PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2025,
            owner_name="Owner",
        )
        table = BrazosPropertySnapshot._meta.db_table

        with CaptureQueriesContext(connection) as queries:
            projection = BrazosActiveSnapshotReadiness().project("000000010013")

        snapshot_reads = [query for query in queries if table in query["sql"]]
        self.assertEqual(len(snapshot_reads), 1)
        self.assertEqual(projection.snapshot_id, snapshot.id)


class AdapterComparableReadingTests(TestCase):
    """Search rows, the subject and the comparables lookup read one active snapshot."""

    def snapshot_reads(self, call):
        table = BrazosPropertySnapshot._meta.db_table
        with CaptureQueriesContext(connection) as queries:
            result = call()
        return result, len([query for query in queries if table in query["sql"]])

    def test_subject_lookup_reads_the_active_snapshot_once(self):
        subject = build_brazos_residential_scenario()

        found, reads = self.snapshot_reads(lambda: adapter.get_subject(subject))

        self.assertTrue(found.has_location)
        self.assertEqual(reads, 1)

    def test_comparables_lookup_reads_one_snapshot_for_subject_and_peers(self):
        subject = build_brazos_residential_scenario()

        comps, reads = self.snapshot_reads(
            lambda: adapter.find_comps(
                subject, max_distance_miles=10.0, max_results=50, min_score=30.0
            )
        )

        self.assertTrue(comps)
        self.assertEqual(reads, 1)

    def test_search_rows_and_subject_lookup_never_run_a_comparables_search(self):
        subject = build_brazos_residential_scenario()
        forbidden = AssertionError("comparables search must not run")

        with (
            patch("counties.brazos.similarity.find_similar_properties", side_effect=forbidden),
            patch("counties.brazos.adapter.find_similar_properties", side_effect=forbidden),
            patch("counties.brazos.readiness.find_similar_properties", side_effect=forbidden),
        ):
            response = self.client.get(reverse("brazos_index"), {"owner_name": "OWNER"})
            found = adapter.get_subject(subject)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(row["protest_url"] for row in response.context["results"]))
        self.assertTrue(found.has_location)


NO_FACTS = "The active property lacks enough comparable facts."
NO_COORDINATES = "Coordinates with source provenance are unavailable."
NO_OWNER = "The active property record has no owner or address."
REPORT = "At least three same-mode comparable-ready properties are required."
TAX = "Report-ready evidence and matching-year tax inputs are required."


class BrazosRecordFactsTests(TestCase):
    """Per-record readiness facts, pinned literally before they were gathered in bulk."""

    def setUp(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2025, outcome=SnapshotOutcome.PARTIAL, cad_source_year=2025
        )

    def account(self, prop_id, **overrides):
        fields = {
            "prop_id": prop_id,
            "tax_year": 2025,
            "owner_name": f"Owner {prop_id}",
            "latitude": Decimal("30.6700000"),
            "longitude": Decimal("-96.3700000"),
            "coordinate_source": "bcad-certified-gis",
            "coordinate_source_year": 2025,
            "living_area": Decimal("2000"),
            "class_code": "RV3",
            **overrides,
        }
        return PropertyAccount.objects.create(**fields)

    def improvement(self, prop_id, imp_id, *, kind="R", year_built=None, tax_year=2025):
        PropertyImprovement.objects.create(
            prop_id=prop_id,
            imp_id=imp_id,
            tax_year=tax_year,
            improvement_type=kind,
            year_built=year_built,
        )

    def building(self, prop_id, imp_id, *, bedrooms=None, bathrooms=None, tax_year=2025):
        PropertyBuildingCharacteristic.objects.create(
            prop_id=prop_id,
            imp_id=imp_id,
            tax_year=tax_year,
            bedrooms=bedrooms,
            bathrooms=bathrooms,
        )

    def build_matrix(self):
        """Each residential-area account has the class fact; one more fact decides its mode."""
        self.account("class-only")
        # "10" sorts before "9": the first residential improvement has no year built.
        self.account("first-imp-by-id")
        self.improvement("first-imp-by-id", "9", year_built=1990)
        self.improvement("first-imp-by-id", "10")
        # A non-residential improvement never supplies residential facts.
        self.account("non-residential-imp")
        self.improvement("non-residential-imp", "1", kind="C", year_built=2000)
        self.improvement("non-residential-imp", "5")
        # Two properties share one improvement id; only one has room facts.
        self.account("shared-imp-bare")
        self.improvement("shared-imp-bare", "77")
        self.building("shared-imp-bare", "77", bedrooms=4, tax_year=2024)
        self.account("shared-imp-rooms")
        self.improvement("shared-imp-rooms", "77")
        self.building("shared-imp-rooms", "77", bedrooms=3)
        self.account("null-rooms")
        self.improvement("null-rooms", "1")
        self.building("null-rooms", "1")
        self.account("baths-only")
        self.improvement("baths-only", "1")
        self.building("baths-only", "1", bathrooms=Decimal("2.00"))
        self.account("imp-year-built")
        self.improvement("imp-year-built", "1", year_built=1975)
        self.account("account-year-built", year_built=1980)
        self.account("land-fact")
        PropertyLand.objects.create(
            prop_id="land-fact", tax_year=2025, land_seq=1, acreage=Decimal("0.2500")
        )
        PropertyLand.objects.create(
            prop_id="prior-year-land", tax_year=2024, land_seq=1, acreage=Decimal("1.0000")
        )
        self.account("prior-year-land")
        self.account("features")
        PropertyExtraFeature.objects.create(
            prop_id="features", imp_id="1", tax_year=2025, feature_type="POOL"
        )
        self.account("land-only", living_area=None, class_code="")
        PropertyLand.objects.create(
            prop_id="land-only", tax_year=2025, land_seq=1, acreage=Decimal("1.0000")
        )
        PropertyLand.objects.create(prop_id="land-only", tax_year=2025, land_seq=2, acreage=None)
        self.account("null-acreage", living_area=None)
        PropertyLand.objects.create(prop_id="null-acreage", tax_year=2025, land_seq=1, acreage=None)
        self.account("no-coordinates", latitude=None, year_built=1980)
        self.account("no-provenance", coordinate_source="", year_built=1980)
        self.account("blank-owner", owner_name="  ", year_built=1980)

    expected = {
        "class-only": (True, None, (NO_FACTS,)),
        "first-imp-by-id": (True, None, (NO_FACTS,)),
        "non-residential-imp": (True, None, (NO_FACTS,)),
        "shared-imp-bare": (True, None, (NO_FACTS,)),
        "shared-imp-rooms": (True, "residential", ()),
        "null-rooms": (True, None, (NO_FACTS,)),
        "baths-only": (True, "residential", ()),
        "imp-year-built": (True, "residential", ()),
        "account-year-built": (True, "residential", ()),
        "land-fact": (True, "residential", ()),
        "prior-year-land": (True, None, (NO_FACTS,)),
        "features": (True, "residential", ()),
        "land-only": (True, "land", ()),
        "null-acreage": (True, None, (NO_FACTS,)),
        "no-coordinates": (True, None, (NO_COORDINATES,)),
        "no-provenance": (True, None, (NO_COORDINATES,)),
        "blank-owner": (False, "residential", ()),
    }

    def assert_matches_expected(self, projection):
        search_ready, mode, comparable_reason = self.expected[projection.prop_id]
        reasons = []
        if not search_ready:
            reasons.append(("search", NO_OWNER))
        reasons += [("comparable", reason) for reason in comparable_reason]
        reasons += [("report", REPORT), ("tax", TAX)]
        self.assertEqual(
            (
                projection.search_ready,
                projection.comparison_mode,
                projection.comparable_ready,
                projection.report_ready,
                projection.tax_impact_ready,
                projection.reasons,
            ),
            (search_ready, mode, mode is not None, False, False, tuple(reasons)),
            projection.prop_id,
        )

    def test_static_projection_judges_each_record_from_its_own_facts(self):
        self.build_matrix()
        readiness = BrazosActiveSnapshotReadiness()
        for prop_id in self.expected:
            self.assert_matches_expected(readiness.project_static(prop_id))

    def test_chunked_projections_equal_each_single_projection(self):
        self.build_matrix()
        readiness = BrazosActiveSnapshotReadiness()
        accounts = list(PropertyAccount.objects.order_by("prop_id"))
        singles = [readiness.project_static(account.prop_id) for account in accounts]

        for chunk_size in (1, 2, len(accounts)):
            with self.subTest(chunk_size=chunk_size):
                chunked = list(readiness.static_projections(accounts, chunk_size=chunk_size))
                self.assertEqual(chunked, singles)
        for projection in singles:
            self.assert_matches_expected(projection)

    def test_a_chunk_costs_the_same_queries_for_one_record_or_many(self):
        self.build_matrix()
        bare = self.account("bare", class_code="")
        readiness = BrazosActiveSnapshotReadiness()
        snapshot = readiness.active_snapshot()
        accounts = list(PropertyAccount.objects.order_by("prop_id"))

        with CaptureQueriesContext(connection) as one:
            list(readiness.static_projections([bare], snapshot=snapshot))
        with CaptureQueriesContext(connection) as many:
            list(readiness.static_projections(accounts, snapshot=snapshot))

        self.assertGreater(len(one), 0)
        self.assertEqual(len(many), len(one))


class BrazosTaxReadinessTests(TestCase):
    """Tax readiness names the exact missing input, and units that levy nothing need no rate."""

    def setUp(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
        )
        for index, prop_id in enumerate([TARGET, "000000020001", "000000020002", "000000020003"]):
            PropertyAccount.objects.create(
                prop_id=prop_id,
                tax_year=2025,
                owner_name=f"OWNER {index}",
                situs_address=f"{100 + index} MAIN ST",
                situs_zip="77801",
                latitude=Decimal("30.6700000") + Decimal(index) / Decimal("100000"),
                longitude=Decimal("-96.3700000"),
                coordinate_source="bcad-certified-gis",
                coordinate_source_year=2025,
                living_area=Decimal("2000"),
                assessed_value=Decimal("300000") if index == 0 else Decimal("250000"),
                class_code="RV3",
            )
            PropertyLand.objects.create(
                prop_id=prop_id, tax_year=2025, land_seq=1, acreage=Decimal("0.2500")
            )
        TaxUnitRate.objects.create(
            county="brazos", tax_year=2025, tax_unit_code="S1", adopted_rate=Decimal("0.011")
        )

    def unit(self, code, name, exemption_code="", amount=None):
        PropertyJurisdictionExemption.objects.create(
            county="brazos",
            tax_year=2025,
            account_number=TARGET,
            tax_unit_code=code,
            tax_unit_name=name,
            exemption_code=exemption_code,
            exemption_amount=amount,
            taxable_value=None if exemption_code else Decimal("300000"),
        )

    def test_units_that_levy_no_tax_need_no_rate(self):
        self.unit("S1", "BRYAN ISD")
        self.unit("CAD", "BRAZOS CENTRAL APPRAISAL DISTRICT")
        self.unit("ZRFND", "BCAD REFUND")
        self.unit("TZ21B", "BRYAN TAX INCREMENT ZONE #21")
        projection = BrazosActiveSnapshotReadiness().project(TARGET)
        self.assertTrue(projection.tax_impact_ready, projection.reason_for("tax"))
        tax = adapter.tax_impact(TARGET, 2025, Decimal("250000"))
        self.assertEqual(tax.completeness, "complete")
        self.assertEqual(tax.current_tax_owed, Decimal("3300.00"))
        warnings = {row["tax_unit_code"]: row["warning"] for row in tax.per_unit_breakdown}
        self.assertIn("levies no tax", warnings["TZ21B"])
        self.assertIn("levies no tax", warnings["CAD"])

    def test_a_property_without_matching_year_rows_says_which_year(self):
        self.assertEqual(
            BrazosActiveSnapshotReadiness().project(TARGET).reason_for("tax"),
            "No 2025 jurisdiction and exemption rows for this property",
        )

    def test_report_names_each_missing_tax_input(self):
        TaxUnitRate.objects.create(
            county="brazos", tax_year=2025, tax_unit_code="G1", adopted_rate=Decimal("0.004")
        )
        self.unit("S1", "BRYAN ISD")
        self.unit("S2", "COLLEGE STATION ISD")
        self.unit("G1", "BRAZOS COUNTY")
        self.unit("G1", "BRAZOS COUNTY", exemption_code="UNVERIFIED")
        self.unit("W9", "", exemption_code="HS", amount=Decimal("100000"))
        gaps = (
            "2025 exemption amounts are unverified for taxing unit G1 (BRAZOS COUNTY); "
            "2025 gross jurisdiction base unavailable for taxing unit W9; "
            "Adopted 2025 rate unavailable for taxing units S2 (COLLEGE STATION ISD), W9"
        )
        self.assertEqual(BrazosActiveSnapshotReadiness().project(TARGET).reason_for("tax"), gaps)
        response = self.client.get(reverse("brazos_protest_analysis", args=[TARGET]))
        self.assertEqual(response.context["tax_impact"].completeness, "missing")
        self.assertContains(response, "COLLEGE STATION ISD")
        self.assertEqual(response.context["tax_impact"].warnings, [gaps])
