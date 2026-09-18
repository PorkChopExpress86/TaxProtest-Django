"""Source-aware coverage through real Brazos candidate preparation."""

import tempfile
from pathlib import Path

from django.db import connection
from django.test import TransactionTestCase

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount, SnapshotOutcome
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
)
from counties.brazos.tests.test_property_import import _stage_complete_pacs_export
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

    def test_first_import_and_zero_search_readiness_are_distinct(self):
        for owners in (True, False):
            with self.subTest(owners=owners), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                write_pacs(root, ["000000010013"], owners=owners)
                result = self.prepare(root)
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                self.assertEqual(candidate.state, "awaiting_review" if owners else "blocked")
                self.assertEqual(bool(candidate.evidence["coverage"]["hard_failures"]), not owners)
                self.assertFalse(PropertyAccount.objects.exists())

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
