"""Source-aware coverage through real Brazos candidate preparation."""

import tempfile
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.models import (
    BrazosPropertySnapshot,
    PropertyAccount,
    PropertyLand,
    SnapshotOutcome,
)
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
    outcome_populations,
)
from counties.brazos.tests.test_property_import import _stage_complete_pacs_export
from counties.common.import_coverage import OutcomePopulation, compare_coverage
from counties.common.models import ImportCandidate
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate


def write_pacs(root, identities, *, owners=True, equity=False):
    _stage_complete_pacs_export(root, 2026)
    for source in (root / "extracted" / "2026").glob("*.TXT"):
        original = source.read_text().rstrip("\n")
        records = []
        for key in identities:
            line = list(key + original[12:])
            if source.name == "APPRAISAL_INFO.TXT" and owners:
                line[608:623] = list("Candidate owner")
            if equity and source.name == "APPRAISAL_ENTITY_INFO.TXT":
                line[148:163] = list("000000000250000")
                line[163:178] = list("000000000250000")
            records.append("".join(line))
        source.write_text("\n".join(records) + "\n")


def write_gis(root, identities, *, equity=True):
    import geopandas as gpd
    from shapely.geometry import Point

    shape = root / "extracted" / "gis" / "2026" / "parcels.shp"
    shape.parent.mkdir(parents=True)
    fields = {"PROP_ID": [int(key) for key in identities]}
    if equity:
        fields.update(
            {
                "living_are": [1800.0] * len(identities),
                "class_cd": ["RV3"] * len(identities),
                "yr_built": [1980] * len(identities),
            }
        )
    gpd.GeoDataFrame(
        fields,
        geometry=[Point(3556000 + i, 10120000) for i in range(len(identities))],
        crs="EPSG:2277",
    ).to_file(shape)


class BrazosCoverageSurveyStressTests(TestCase):
    """Coverage qualification surveys a full snapshot within fixed query and time caps."""

    def build_snapshot(self, count):
        BrazosPropertySnapshot.objects.create(
            tax_year=2026,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2026,
            gis_source_year=2026,
        )
        accounts, lands = [], []
        for index in range(count):
            prop_id = str(index + 10000).zfill(12)
            # Even accounts compare as residential; odd ones by land area alone.
            residential = index % 2 == 0
            accounts.append(
                PropertyAccount(
                    prop_id=prop_id,
                    tax_year=2026,
                    owner_name=f"Owner {index}",
                    latitude=Decimal("30.6") + Decimal(index) / Decimal("1000"),
                    longitude=Decimal("-96.3"),
                    coordinate_source="bcad-certified-gis",
                    coordinate_source_year=2026,
                    living_area=Decimal("1800"),
                    assessed_value=Decimal("250000"),
                    class_code="RV3" if residential else "",
                    year_built=1980 if residential else None,
                )
            )
            if not residential:
                lands.append(
                    PropertyLand(prop_id=prop_id, tax_year=2026, land_seq=1, acreage=Decimal("0.5"))
                )
        PropertyAccount.objects.bulk_create(accounts)
        PropertyLand.objects.bulk_create(lands)

    def survey_cost(self):
        forbidden = AssertionError("coverage must not run a comparables search")
        with (
            patch("counties.brazos.similarity.find_similar_properties", side_effect=forbidden),
            patch("counties.brazos.adapter.find_similar_properties", side_effect=forbidden),
            CaptureQueriesContext(connection) as queries,
        ):
            start = time.monotonic()
            populations = outcome_populations(claimed_gis=True)
            duration = time.monotonic() - start
        return populations, len(queries), duration

    def test_a_few_thousand_accounts_qualify_within_query_and_time_caps(self):
        count = 4000
        self.build_snapshot(count)

        populations, queries, duration = self.survey_cost()

        self.assertEqual(len(populations["report"].eligible), count)
        self.assertEqual(len(populations["comparable"].eligible), count)
        self.assertFalse(populations["tax"].eligible)
        # Two chunks of 2,000: a fixed number of reads per chunk, never per account.
        self.assertLessEqual(queries, 40)
        self.assertLess(duration, 30.0, f"Coverage took {duration:.1f}s for {count} accounts")

    def test_report_is_unsupported_when_no_comparison_mode_reaches_the_report_pool(self):
        # Four comparable-ready properties with equity facts, but split two per
        # comparison mode: neither mode holds a subject plus three others.
        self.build_snapshot(4)

        populations, _, _ = self.survey_cost()

        self.assertEqual(len(populations["comparable"].eligible), 4)
        self.assertFalse(populations["report"].eligible)
        self.assertFalse(populations["report"].supported)
        self.assertEqual(
            populations["report"].reason,
            "Equity facts and at least three qualifying comparables are required",
        )
        previous = {name: OutcomePopulation(set()) for name in populations}
        failures = compare_coverage(previous, populations)["hard_failures"]
        self.assertNotIn("report: zero eligible records for a supported outcome", failures)

    def test_report_is_supported_once_one_comparison_mode_reaches_the_report_pool(self):
        self.build_snapshot(7)

        populations, _, _ = self.survey_cost()

        self.assertEqual(len(populations["report"].eligible), 4)
        self.assertTrue(populations["report"].supported)

    def test_coverage_queries_grow_only_with_chunks(self):
        self.build_snapshot(8)
        _, few, _ = self.survey_cost()
        PropertyLand.objects.all().delete()
        PropertyAccount.objects.all().delete()
        BrazosPropertySnapshot.objects.all().delete()
        self.build_snapshot(1500)

        _, many, _ = self.survey_cost()

        self.assertEqual(many, few)


class BrazosCoverageTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.filter(county="brazos"):
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def prepare(self, root, *, annual=False):
        with self.settings(
            BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
        ):
            return BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(
                PropertyImportRequest(
                    mode=PropertyImportMode.ANNUAL if annual else PropertyImportMode.CAD_RECOVERY,
                    options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
                    prepare_only=True,
                )
            )

    def test_partial_one_percent_boundary_and_growth_masking(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
        )
        identities = [str(number + 10000).zfill(12) for number in range(100)]
        PropertyAccount.objects.bulk_create(
            [
                PropertyAccount(prop_id=key, tax_year=2025, owner_name="Published owner")
                for key in identities
            ]
        )
        for missing in (1, 2):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                write_pacs(
                    root,
                    identities[missing:] + [str(number + 20000).zfill(12) for number in range(100)],
                )
                result = self.prepare(root)
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                coverage = candidate.evidence["coverage"]
                self.assertEqual(coverage["requires_review"], missing == 2)
                self.assertEqual(coverage["outcomes"]["search"]["unexplained_loss"], missing)
                self.assertEqual(coverage["outcomes"]["search"]["additions"], 100)
                self.assertTrue(coverage["outcomes"]["comparable"]["deliberately_absent"])
                self.assertFalse(coverage["outcomes"]["comparable"]["supported"])
                self.assertEqual(PropertyAccount.objects.count(), 100)

    def test_annual_measures_report_prerequisites_and_coordinate_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identities = [str(number + 10013).zfill(12) for number in range(4)]
            write_pacs(root, identities, equity=True)
            write_gis(root, identities)
            result = self.prepare(root, annual=True)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            outcomes = candidate.evidence["coverage"]["outcomes"]
            self.assertEqual(outcomes["comparable"]["total_ready"], 4)
            self.assertEqual(outcomes["report"]["total_ready"], 4)
            self.assertFalse(outcomes["tax"]["supported"])
            self.assertTrue(candidate.evidence["coordinate_provenance"])
            self.assertFalse(BrazosPropertySnapshot.objects.exists())

    def test_prior_tax_readiness_does_not_make_new_year_tax_gap_a_property_block(self):
        identities = [str(number + 10013).zfill(12) for number in range(4)]
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=SnapshotOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
        )
        TaxUnitRate.objects.create(
            county="brazos", tax_year=2025, tax_unit_code="C", adopted_rate="0.01"
        )
        for key in identities:
            PropertyAccount.objects.create(
                prop_id=key,
                tax_year=2025,
                owner_name="Published owner",
                assessed_value=250000,
                living_area=1800,
                class_code="RV3",
                year_built=1980,
                latitude=30.6,
                longitude=-96.3,
                coordinate_source="bcad-certified-gis",
                coordinate_source_year=2025,
            )
            PropertyJurisdictionExemption.objects.create(
                county="brazos",
                tax_year=2025,
                account_number=key,
                tax_unit_code="C",
                taxable_value=250000,
            )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pacs(root, identities, equity=True)
            write_gis(root, identities)
            result = self.prepare(root, annual=True)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            coverage = candidate.evidence["coverage"]
            self.assertEqual(coverage["outcomes"]["tax"]["prior_ready"], 4)
            self.assertFalse(coverage["outcomes"]["tax"]["supported"])
            self.assertTrue(coverage["automatic_publication_allowed"])
            self.assertEqual(BrazosPropertySnapshot.objects.get(is_active=True).tax_year, 2025)
