"""County fixtures for Candidate lifecycle contract tests: each binds one real county port."""

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import connection

from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.models import PropertyAccount
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
)
from counties.brazos.tests.test_property_coverage import write_pacs
from counties.common.import_review import captured_binding, review_candidate
from counties.common.models import ImportCandidate
from counties.harris.etl_pipeline import run_harris_import
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
from counties.harris.etl_pipeline.tests.test_import_publication import harris_request
from counties.harris.models import PropertyRecord


class CandidateContract:
    """A first-import candidate awaiting review, prepared through one county's real import."""

    county: str
    candidate_column: tuple[str, str]  # a staged table and one of its text columns

    def prepare(self, root: Path) -> ImportCandidate:
        raise NotImplementedError

    def add_published_record(self, key: str):
        raise NotImplementedError

    def published_keys(self) -> list[str]:
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.add_published_record("PUBLISHED")
        self.candidate = self.prepare(self.root)
        self.assertEqual(self.candidate.state, "awaiting_review")
        self.reviewer = get_user_model().objects.create_superuser("reviewer", password="test")

    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def approve(self):
        return review_candidate(
            self.candidate,
            user=self.reviewer,
            reason="Reviewed exact candidate evidence",
            decision="approved",
            expected_binding=captured_binding(self.candidate),
        )


class HarrisCandidateContract(CandidateContract):
    county = "harris"
    candidate_column = ("data_propertyrecord", "address")

    def prepare(self, root):
        self.enterContext(self.settings(**_runtime_settings(root)))
        result = run_harris_import(harris_request(root, prepare=True))
        return ImportCandidate.objects.get(pk=result.candidate_id)

    def add_published_record(self, key):
        PropertyRecord.objects.create(account_number=key)

    def published_keys(self):
        return sorted(PropertyRecord.objects.values_list("account_number", flat=True))


class BrazosCandidateContract(CandidateContract):
    county = "brazos"
    candidate_column = ("brazos_cad_propertyaccount", "owner_name")

    def prepare(self, root):
        write_pacs(root, ["000000010013"])
        self.enterContext(
            self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                BCAD_EXTRACT_DIR=str(root / "extracted"),
            )
        )
        result = BrazosPropertyImport(CadRefreshStage(), None).run(
            PropertyImportRequest(
                mode=PropertyImportMode.CAD_RECOVERY,
                options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
                prepare_only=True,
            )
        )
        return ImportCandidate.objects.get(pk=result.candidate_id)

    def add_published_record(self, key):
        PropertyAccount.objects.create(tax_year=2025, prop_id=key, owner_name="Live owner")

    def published_keys(self):
        return sorted(PropertyAccount.objects.values_list("prop_id", flat=True))
