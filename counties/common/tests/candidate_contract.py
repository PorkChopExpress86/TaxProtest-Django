"""County fixtures for Candidate lifecycle contract tests: each binds one real county port."""

import tempfile
from dataclasses import replace
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import connection

from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount, SnapshotOutcome
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
    brazos_candidate_load,
)
from counties.brazos.tests.test_property_coverage import write_pacs
from counties.common.candidate_lifecycle import CandidateLoad, Loaded
from counties.common.import_audit import record_sources
from counties.common.import_review import captured_binding, review_candidate
from counties.common.models import ImportCandidate
from counties.harris.etl_pipeline import run_harris_import
from counties.harris.etl_pipeline.orchestrator import harris_candidate_load
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
from counties.harris.etl_pipeline.tests.test_import_publication import harris_request
from counties.harris.models import BuildingDetail, PropertyRecord


class CandidateContract:
    """A first-import candidate awaiting review, prepared through one county's real import."""

    county: str
    candidate_column: tuple[str, str]  # a staged table and one of its text columns

    def prepare(self, root: Path) -> ImportCandidate:
        raise NotImplementedError

    def candidate_load(self) -> CandidateLoad:
        """The county's real load for a default request."""
        raise NotImplementedError

    def write_candidate_rows(self, *, ready: bool):
        """Write a few county rows into the tables the search path points at."""
        raise NotImplementedError

    def add_published_record(self, key: str):
        raise NotImplementedError

    def published_keys(self) -> list[str]:
        raise NotImplementedError

    def fixture_load(self, *, ready=True, failure: Exception | None = None) -> CandidateLoad:
        """The county's real load with only its load step replaced by a fixture writer."""
        source = self.root / "fixture-source.txt"

        def write(candidate, operation):
            source.write_text(f"fixture rows for {candidate.pk}")
            record_sources(operation, [source])
            operation.evidence["validation"] = {"valid": True}
            if failure is not None:
                raise failure
            self.write_candidate_rows(ready=ready)
            return Loaded(complete=True, evidence={"fixture": "rows written"})

        return replace(self.candidate_load(), run=write)

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

    def candidate_load(self):
        return harris_candidate_load(harris_request(self.root, prepare=True))

    def write_candidate_rows(self, *, ready):
        for number in range(4):
            prop, _ = PropertyRecord.objects.update_or_create(
                account_number=f"C{number}",
                defaults={
                    "is_residential": ready,
                    "is_data_ready": ready,
                    "latitude": 29.7,
                    "longitude": -95.4,
                    "assessed_value": 250000,
                },
            )
            BuildingDetail.objects.update_or_create(
                property=prop,
                building_number=1,
                defaults={"account_number": prop.account_number, "heat_area": 1800},
            )

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

    def candidate_load(self):
        return brazos_candidate_load(
            CadRefreshStage(),
            None,
            PropertyImportRequest(
                mode=PropertyImportMode.CAD_RECOVERY,
                options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
                prepare_only=True,
            ),
        )

    def write_candidate_rows(self, *, ready):
        BrazosPropertySnapshot.objects.update_or_create(
            tax_year=2026,
            defaults={
                "cad_source_year": 2026,
                "outcome": SnapshotOutcome.PARTIAL,
                "is_active": True,
            },
        )
        for number in range(4):
            PropertyAccount.objects.update_or_create(
                tax_year=2026,
                prop_id=f"C{number}",
                defaults={"owner_name": "Candidate owner" if ready else ""},
            )

    def add_published_record(self, key):
        PropertyAccount.objects.create(tax_year=2025, prop_id=key, owner_name="Live owner")

    def published_keys(self):
        return sorted(PropertyAccount.objects.values_list("prop_id", flat=True))
