"""Observe qualified Brazos publication through imports and admin."""

import csv
import io
import tempfile
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount, SnapshotOutcome
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
)
from counties.brazos.tests.test_property_coverage import write_gis, write_pacs
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate


class BrazosPublicationTests(TransactionTestCase):
    def test_imported_gross_base_is_exempted_once_in_shared_report_and_exports(self):
        self.client.force_login(
            get_user_model().objects.create_superuser("gross-reviewer", password="test")
        )
        other = PropertyJurisdictionExemption.objects.create(
            county="harris",
            account_number="H1",
            tax_year=2026,
            tax_unit_code="G1",
            taxable_value=333333,
        )
        TaxUnitRate.objects.create(
            county="brazos",
            tax_year=2026,
            tax_unit_code="G1",
            adopted_rate=Decimal("0.01"),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = [f"{10013 + i:012d}" for i in range(4)]
            write_pacs(root, ids, equity=True)
            write_gis(root, ids)
            source = root / "extracted" / "2026" / "APPRAISAL_ENTITY_INFO.TXT"
            lines = []
            for raw in source.read_text().splitlines():
                line = list(raw)
                line[148:163] = list("000000000242613")
                line[163:178] = list("000000000167613")
                line[313:328] = list("000000000075000")
                lines.append("".join(line))
            source.write_text("\n".join(lines) + "\n")
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                BCAD_EXTRACT_DIR=str(root / "extracted"),
            ):
                result = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(
                    self.request(annual=True)
                )
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                url = reverse("admin:data_importcandidate_review", args=[candidate.pk])
                binding = self.client.get(url).context["form"]["binding"].value()
                self.assertEqual(
                    self.client.post(
                        url,
                        {
                            "decision": "approved",
                            "reason": "Verified gross source and exemptions",
                            "binding": binding,
                        },
                    ).status_code,
                    302,
                )
                self.assertEqual(
                    self.client.post(
                        reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                        {"reason": "Publish verified gross base"},
                    ).status_code,
                    302,
                )
                report = self.client.get(reverse("brazos_protest_analysis", args=[ids[0]]))
                self.assertEqual(report.status_code, 200)
                tax = report.context["tax_impact"]
                self.assertEqual(tax.completeness, "complete")
                self.assertEqual(tax.current_tax_owed, Decimal("1676.13"))
                exported = self.client.get(reverse("brazos_protest_analysis_export", args=[ids[0]]))
                rows = list(csv.DictReader(io.StringIO(exported.content.decode())))
                self.assertTrue(rows)
                self.assertEqual(Decimal(rows[0]["current_tax_owed"]), Decimal("1676.13"))
                pdf = self.client.get(reverse("brazos_protest_analysis_pdf", args=[ids[0]]))
                self.assertIn(b"1,676.13", pdf.content)
                other.refresh_from_db()
                self.assertEqual(other.taxable_value, 333333)
                # An unverified reduction (for example DV) remains unavailable.
                for index, raw in enumerate(lines):
                    line = list(raw)
                    line[163:178] = list("000000000000000")
                    lines[index] = "".join(line)
                source.write_text("\n".join(lines) + "\n")
                changed = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(
                    self.request(annual=True)
                )
                self.assertEqual(changed.workflow_state, "published")
                report = self.client.get(reverse("brazos_protest_analysis", args=[ids[0]]))
                self.assertNotEqual(report.context["tax_impact"].completeness, "complete")
                self.assertContains(report, "Tax totals unavailable")
                self.assertContains(report, "unverified")
                other.refresh_from_db()
                self.assertEqual(other.taxable_value, 333333)

    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.filter(county="brazos"):
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def request(self, *, annual=False, prepare=False):
        return PropertyImportRequest(
            mode=PropertyImportMode.ANNUAL if annual else PropertyImportMode.CAD_RECOVERY,
            options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
            prepare_only=prepare,
        )

    def baseline(self):
        PropertyAccount.objects.create(
            tax_year=2026, prop_id="000000010013", owner_name="Old owner"
        )
        return BrazosPropertySnapshot.objects.create(
            tax_year=2026, cad_source_year=2026, outcome=SnapshotOutcome.PARTIAL, is_active=True
        )

    def test_same_year_qualified_partial_publication_and_repeat_are_safe(self):
        old = self.baseline()
        harris = PropertyJurisdictionExemption.objects.create(
            county="harris",
            tax_year=2026,
            account_number="H1",
            tax_unit_code="H",
            taxable_value=100,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pacs(root, ["000000010013"])
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                importer = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage())
                request = replace(
                    self.request(), options=replace(self.request().options, tax_year=None)
                )
                result = importer.run(request)
                self.assertEqual(result.workflow_state, "published")
                self.assertFalse(result.prepared)
                self.assertNotEqual(result.snapshot_id, old.pk)
                self.assertEqual(PropertyAccount.objects.get().owner_name, "Candidate owner")
                repeated = importer.run(replace(request, candidate_id=result.candidate_id))
                self.assertTrue(repeated.already_applied)
                self.assertEqual(
                    ImportAuditEntry.objects.filter(kind="publication", result="published").count(),
                    1,
                )
                self.assertEqual(BrazosPropertySnapshot.objects.filter(is_active=True).count(), 1)
                harris.refresh_from_db()
                self.assertEqual(harris.taxable_value, 100)

    def test_first_annual_requires_review_and_explicit_apply(self):
        self.client.force_login(
            get_user_model().objects.create_superuser("operator", password="test")
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = [f"{10013 + i:012d}" for i in range(4)]
            write_pacs(root, ids, equity=True)
            write_gis(root, ids)
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                result = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(
                    replace(
                        self.request(annual=True),
                        options=replace(self.request().options, tax_year=None),
                    )
                )
                self.assertEqual(result.workflow_state, "awaiting_review")
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                review_url = reverse("admin:data_importcandidate_review", args=[candidate.pk])
                binding = self.client.get(review_url).context["form"]["binding"].value()
                self.assertEqual(
                    self.client.post(
                        review_url,
                        {
                            "decision": "approved",
                            "reason": "Annual source years verified",
                            "binding": binding,
                        },
                    ).status_code,
                    302,
                )
                self.assertFalse(BrazosPropertySnapshot.objects.exists())
                self.assertEqual(
                    self.client.post(
                        reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                        {"reason": "Publish reviewed annual data"},
                    ).status_code,
                    302,
                )
                active = BrazosPropertySnapshot.objects.get(is_active=True)
                self.assertEqual(active.outcome, SnapshotOutcome.COMPLETED)
                self.assertEqual(active.gis_source_year, 2026)
                self.assertEqual(PropertyAccount.objects.count(), 4)
                self.assertEqual(candidate.request["tax_year"], 2026)

    def test_gis_recovery_without_explicit_year_can_be_reviewed_and_applied(self):
        self.baseline()
        self.client.force_login(
            get_user_model().objects.create_superuser("operator", password="test")
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_gis(root, ["000000010013"])
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                request = replace(
                    self.request(),
                    mode=PropertyImportMode.GIS_RECOVERY,
                    options=replace(self.request().options, tax_year=None),
                )
                result = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(request)
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                self.assertEqual(candidate.request["tax_year"], 2026)
                review_url = reverse("admin:data_importcandidate_review", args=[candidate.pk])
                binding = self.client.get(review_url).context["form"]["binding"].value()
                self.assertEqual(
                    self.client.post(
                        review_url,
                        {
                            "decision": "approved",
                            "reason": "Coordinate prerequisite verified",
                            "binding": binding,
                        },
                    ).status_code,
                    302,
                )
                self.assertEqual(
                    self.client.post(
                        reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                        {"reason": "Apply GIS recovery"},
                    ).status_code,
                    302,
                )
                self.assertEqual(
                    BrazosPropertySnapshot.objects.get(is_active=True).outcome,
                    SnapshotOutcome.COMPLETED,
                )

    def test_failed_publication_audit_rolls_back_all_public_facts(self):
        old = self.baseline()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pacs(root, ["000000010013"])
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                importer = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage())
                result = importer.run(self.request(prepare=True))

                def fail(execute, sql, params, many, context):
                    if sql.startswith('INSERT INTO "data_importauditentry"'):
                        raise OSError("Final publication audit failed")
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(fail), self.assertRaises(OSError):
                    importer.run(replace(self.request(), candidate_id=result.candidate_id))
                self.assertEqual(PropertyAccount.objects.get().owner_name, "Old owner")
                self.assertEqual(BrazosPropertySnapshot.objects.get(is_active=True).pk, old.pk)
                self.assertFalse(ImportOperation.objects.filter(status="published").exists())

    def test_gis_recovery_rechecks_recorded_partial_identity(self):
        self.baseline()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_gis(root, ["000000010013"])
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                importer = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage())
                request = replace(self.request(prepare=True), mode=PropertyImportMode.GIS_RECOVERY)
                result = importer.run(request)
                BrazosPropertySnapshot.objects.update(is_active=False)
                current = BrazosPropertySnapshot.objects.create(
                    tax_year=2026,
                    cad_source_year=2026,
                    outcome=SnapshotOutcome.PARTIAL,
                    is_active=True,
                )
                with self.assertRaisesMessage(ValueError, "baseline changed"):
                    importer.run(
                        replace(request, prepare_only=False, candidate_id=result.candidate_id)
                    )
                self.assertEqual(BrazosPropertySnapshot.objects.get(is_active=True).pk, current.pk)

    def test_shared_request_keeps_old_active_snapshot_and_details_during_publish(self):
        self.baseline()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pacs(root, ["000000010013"])
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                importer = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage())
                result = importer.run(self.request(prepare=True))
                selected, resume = threading.Event(), threading.Event()
                owners, errors = [], []

                def reader():
                    close_old_connections()

                    def pause(execute, sql, params, many, context):
                        result = execute(sql, params, many, context)
                        if (
                            sql.startswith("SELECT")
                            and 'FROM "brazos_cad_brazospropertysnapshot"' in sql
                            and not selected.is_set()
                        ):
                            selected.set()
                            if not resume.wait(20):
                                raise TimeoutError("Reader was never resumed")
                        return result

                    try:
                        with connection.execute_wrapper(pause):
                            response = Client().get(
                                reverse("brazos_similar_properties", args=["000000010013"])
                            )
                            owners.append(response.context["subject"].owner_name)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        connection.close()

                thread = threading.Thread(target=reader)
                thread.start()
                try:
                    self.assertTrue(selected.wait(20))
                    importer.run(replace(self.request(), candidate_id=result.candidate_id))
                finally:
                    resume.set()
                    thread.join(20)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(owners, ["Old owner"])
                response = self.client.get(
                    reverse("brazos_similar_properties", args=["000000010013"])
                )
                self.assertEqual(response.context["subject"].owner_name, "Candidate owner")

    def test_preview_and_prepare_intents_cannot_apply(self):
        self.baseline()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pacs(root, ["000000010013"])
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                importer = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage())
                result = importer.run(self.request(prepare=True))
                for options in (
                    {"prepare_only": True},
                    {"options": replace(self.request().options, dry_run=True)},
                ):
                    with self.subTest(options=options), self.assertRaises(ValueError):
                        importer.run(
                            replace(self.request(), candidate_id=result.candidate_id, **options)
                        )
                self.assertEqual(PropertyAccount.objects.get().owner_name, "Old owner")

    def test_post_commit_reporting_failure_retains_observed_publication(self):
        self.baseline()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pacs(root, ["000000010013"])
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                importer = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage())
                result = importer.run(self.request(prepare=True))

                def fail(execute, sql, params, many, context):
                    if (
                        connection.get_autocommit()
                        and sql.startswith("SELECT")
                        and 'FROM "brazos_cad_brazospropertysnapshot"' in sql
                        and ImportCandidate.objects.get(pk=result.candidate_id).state == "published"
                    ):
                        raise OSError("Reporting failed after commit")
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(fail):
                    applied = importer.run(
                        replace(self.request(), candidate_id=result.candidate_id)
                    )
                self.assertEqual(applied.workflow_state, "published")
                self.assertTrue(applied.cleanup_warnings)
                self.assertEqual(PropertyAccount.objects.get().owner_name, "Candidate owner")
