"""Persisted import evidence is a contract: its bytes are pinned here (ADR-0024).

Review, approval, publication and recovery hash stored evidence and compare it with
values recorded earlier, so a renamed key, a reworded string or a changed separator
invalidates every in-flight candidate. The expected bytes live beside this module in
``persisted_evidence/`` and were captured from today's real county imports. Only the
atoms that differ on every run (identifiers, the temporary root, dataset digests and
the date-stamped GIS ``.dbf`` digest) are placeholders. Never regenerate these files
to make a failure pass: a difference is a data migration that must rebind stored rows.
"""

import hashlib
import json
import tempfile
from pathlib import Path
from uuid import UUID

from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
)
from counties.brazos.tests.test_property_coverage import write_gis, write_pacs
from counties.common.candidate_lifecycle import apply, recover
from counties.common.import_audit import audited_operation
from counties.common.import_recovery import replay_binding
from counties.common.import_retention import baseline_sources
from counties.common.import_review import captured_binding, checked_binding
from counties.common.models import CountyWriter, ImportAuditEntry, ImportCandidate, ImportOperation
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)

PINNED = Path(__file__).parent / "persisted_evidence"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FixedInputBindingTests(SimpleTestCase):
    """The binding digests themselves, over one fixed candidate."""

    def candidate(self):
        operation = ImportOperation(
            pk=UUID("00000000-0000-4000-8000-000000000002"),
            county="brazos",
            evidence={"validation": {"valid": True}, "sources": []},
        )
        return ImportCandidate(
            pk=UUID("00000000-0000-4000-8000-000000000001"),
            operation=operation,
            county="brazos",
            state="published",
            request={"mode": "cad_recovery", "tax_year": 2026},
            sources=[{"path": "/sources/APPRAISAL_INFO.TXT", "sha256": "ab" * 32}],
            baseline={"sha256": "cd" * 32, "snapshot_id": None},
            evidence={"content_identity": {"sha256": "ef" * 32}, "coverage": {}},
        )

    def test_captured_binding_digest_is_pinned(self):
        # sha256 of the default-separator, sorted-key JSON of candidate, state, request,
        # sources, baseline, evidence and the whole operation evidence as "validation".
        self.assertEqual(
            captured_binding(self.candidate()),
            "9e90b70c46e44a79fa585054d1a7d623555e5bb0e6c2a1cd43bd004248059e1c",
        )

    def test_replay_binding_digest_is_pinned(self):
        # sha256 of the compact, sorted-key JSON of candidate_id, state, request, sources,
        # content_identity and property_source_year (null when the county records none).
        self.assertEqual(
            replay_binding(self.candidate()),
            "09ba2757cce033639ad216c8841f222bfb0e0477c1fbd5a69db4b48f91da89c3",
        )


class PersistedEvidencePins:
    """Real county preparation, review, publication and recovery, pinned byte for byte."""

    def atoms(self, candidate) -> dict[str, str]:
        atoms = {
            "{candidate}": str(candidate.pk),
            "{operation}": str(candidate.operation_id),
            "{root}": str(self.root),
            "{baseline}": candidate.baseline["sha256"],
            "{content}": candidate.evidence["content_identity"]["sha256"],
        }
        dbf = [item["sha256"] for item in candidate.sources if item["path"].endswith(".dbf")]
        if dbf:
            atoms["{gis_dbf}"] = dbf[0]
        return atoms

    def pinned(self, name: str, **atoms: str) -> str:
        text = (PINNED / f"{self.county}_{name}").read_text().strip()
        for placeholder, value in {**self.atoms(self.candidate), **atoms}.items():
            text = text.replace(placeholder, value)
        return text

    def publish(self):
        self.approve()
        apply(self.candidate, user=self.reviewer, reason="Publish first import")
        self.candidate.refresh_from_db()

    def test_checked_binding_hashes_the_pinned_bytes(self):
        self.assertEqual(
            checked_binding(self.candidate), sha256(self.pinned("checked_binding.json"))
        )
        self.approve()
        review = ImportAuditEntry.objects.get(kind="coverage_review", result="approved")
        self.assertEqual(
            review.evidence["qualification_binding"], sha256(self.pinned("checked_binding.json"))
        )

    def test_replay_binding_hashes_the_pinned_bytes(self):
        self.publish()
        self.assertEqual(replay_binding(self.candidate), sha256(self.pinned("replay_binding.json")))

    def test_replay_verification_evidence_is_pinned(self):
        self.publish()
        binding = replay_binding(self.candidate)
        operation = recover(
            self.candidate,
            user=self.reviewer,
            reason="Restore retained sources",
            binding=binding,
        )
        recovery = operation.evidence["recovery"]
        self.assertEqual(
            recovery,
            json.loads(
                self.pinned(
                    "replay_verification.json",
                    **{"{binding}": binding, "{request_id}": recovery["request_id"]},
                )
            ),
        )

    def test_retained_source_records_are_pinned(self):
        evidence = self.candidate.operation.evidence
        self.assertEqual(
            {
                key: evidence[key]
                for key in ("base_sources", "retained_source_digests", "working_sources")
            },
            json.loads(self.pinned("retained_sources.json")),
        )
        self.publish()
        self.assertEqual(
            baseline_sources(self.county), json.loads(self.pinned("inherited_sources.json"))
        )
        publication = ImportAuditEntry.objects.get(kind="publication")
        self.assertEqual(
            sorted(publication.evidence),
            ["after", "before", "county", "operation_id", "review_id", "source_years"],
        )
        self.assertEqual(publication.evidence["source_years"], self.candidate.sources)


class HarrisPersistedEvidenceTests(
    PersistedEvidencePins, HarrisCandidateContract, TransactionTestCase
):
    def test_property_source_year_is_the_recorded_year(self):
        self.assertEqual(self.candidate.evidence["property_source_year"], 2026)
        self.assertIn('"property_source_year":2026', self.pinned("checked_binding.json"))


class BrazosPersistedEvidenceTests(
    PersistedEvidencePins, BrazosCandidateContract, TransactionTestCase
):
    def test_property_source_year_is_present_but_empty(self):
        """Brazos records no property source year, yet every binding carries the key."""
        self.assertNotIn("property_source_year", self.candidate.evidence)
        for name in ("checked_binding.json", "replay_binding.json"):
            self.assertIn('"property_source_year":null', self.pinned(name))
        self.publish()
        operation = recover(
            self.candidate,
            user=self.reviewer,
            reason="Restore retained sources",
            binding=replay_binding(self.candidate),
        )
        self.assertIn("property_source_year", operation.evidence["recovery"])
        self.assertIsNone(operation.evidence["recovery"]["property_source_year"])

    def test_partial_candidate_coverage_records_gis_as_deliberately_absent(self):
        outcomes = json.loads(self.pinned("checked_binding.json"))["coverage"]["outcomes"]
        self.assertEqual(self.candidate.evidence["coverage"]["outcomes"], outcomes)
        self.assertEqual(
            {name: outcome["deliberately_absent"] for name, outcome in outcomes.items()},
            {"search": False, "comparable": True, "report": True, "tax": True},
        )


class BrazosAnnualCoverageEvidenceTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def test_annual_candidate_coverage_shape_is_pinned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identities = [str(number + 10013).zfill(12) for number in range(4)]
            write_pacs(root, identities, equity=True)
            write_gis(root, identities)
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                result = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.ANNUAL,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True
                        ),
                        prepare_only=True,
                    )
                )
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
        self.assertEqual(
            candidate.evidence["coverage"],
            json.loads((PINNED / "brazos_annual_coverage.json").read_text()),
        )
        self.assertNotIn("property_source_year", candidate.evidence)
        self.assertEqual(
            {tuple(sorted(source)) for source in candidate.sources},
            {("path", "sha256", "source_url", "source_year", "stage", "target_year")},
        )
        self.assertEqual({source["stage"] for source in candidate.sources}, {"cad", "gis"})


class CountyWriterLockKeyTests(TransactionTestCase):
    """Running deployments coordinate writers through these exact advisory lock keys."""

    def held_advisory_keys(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT classid, objid, objsubid FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND granted"
            )
            return cursor.fetchall()

    def test_each_county_reserves_its_writer_under_its_pinned_lock_key(self):
        for county, key in (("harris", 742101), ("brazos", 742102)):
            with self.subTest(county=county):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                with audited_operation(county, "lock pin", actor="operator") as operation:
                    writer = CountyWriter.objects.get(county=county)
                    self.assertEqual(writer.operation_id, operation.pk)
                    self.assertEqual(writer.backend_pid, pid)
                    self.assertEqual(self.held_advisory_keys(), [(0, key, 1)])
                self.assertEqual(self.held_advisory_keys(), [])
                writer.refresh_from_db()
                self.assertIsNone(writer.operation_id)
