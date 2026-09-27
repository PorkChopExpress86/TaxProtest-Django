"""Publication behaves identically through the shared Candidate lifecycle for every county."""

import threading

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from counties.common.candidate_lifecycle import publish
from counties.common.candidate_ports import published_identity
from counties.common.import_audit import audited_operation
from counties.common.import_review import ImportReviewRejected
from counties.common.import_writers import WriterConflict, county_writer
from counties.common.models import ImportAuditEntry, ImportOperation
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)

AUDIT_EVIDENCE = {"before", "after", "review_id", "operation_id", "source_years", "county"}


class PublicationContract:
    """Behaviour every county port must give publication."""

    def publish(self) -> ImportOperation:
        with audited_operation(self.county, "apply", actor="operator") as operation:
            publish(operation, self.candidate.pk, user=self.reviewer, reason="Apply reviewed")
        operation.refresh_from_db()
        self.candidate.refresh_from_db()
        return operation

    def test_first_import_requires_review_authorization(self):
        with self.assertRaisesMessage(ImportReviewRejected, "Coverage review approval"):
            self.publish()
        self.assertEqual(self.published_keys(), ["PUBLISHED"])
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.state, "awaiting_review")

    def test_reviewed_candidate_publishes_once_and_repeat_is_already_applied(self):
        self.approve()
        applied = self.publish()
        self.assertEqual(self.candidate.state, "published")
        self.assertEqual(applied.status, "published")
        self.assertEqual(
            applied.publication_after,
            published_identity(self.county) | {"candidate_id": str(self.candidate.pk)},
        )
        self.assertEqual(applied.evidence["application_reason"], "Apply reviewed")
        audit = ImportAuditEntry.objects.get(kind="publication")
        self.assertEqual(set(audit.evidence), AUDIT_EVIDENCE)
        self.assertEqual(audit.evidence["county"], self.candidate.evidence["audit"])
        self.assertEqual(audit.evidence["operation_id"], str(applied.pk))
        self.assertEqual(audit.reason, "Apply reviewed")

        repeated = self.publish()
        self.assertEqual(repeated.status, "already_applied")
        self.assertEqual(repeated.evidence["already_applied"], str(self.candidate.pk))
        self.assertEqual(ImportAuditEntry.objects.filter(kind="publication").count(), 1)

    def test_failed_publication_audit_rolls_back_the_publication(self):
        self.approve()
        before = published_identity(self.county)

        def fail(execute, sql, params, many, context):
            if sql.startswith('INSERT INTO "data_importauditentry"'):
                raise OSError("Final publication audit failed")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail), self.assertRaises(OSError):
            self.publish()
        self.assertEqual(published_identity(self.county), before)
        self.assertEqual(self.published_keys(), ["PUBLISHED"])
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.state, "approved")

    def test_publication_holds_the_writer_and_keeps_previous_rows_readable(self):
        self.approve()
        observed = []

        def observe():
            close_old_connections()
            try:
                observed.append(self.published_keys())
                competing = ImportOperation.objects.create(county=self.county, intent="annual")
                with county_writer(competing):
                    observed.append("acquired")
            except WriterConflict:
                observed.append("rejected")
            finally:
                connection.close()

        def during_cutover(execute, sql, params, many, context):
            if sql.startswith("INSERT INTO public.") and not observed:
                thread = threading.Thread(target=observe)
                thread.start()
                thread.join(20)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(during_cutover):
            self.publish()
        self.assertEqual(observed, [["PUBLISHED"], "rejected"])
        self.assertEqual(self.candidate.state, "published")


class HarrisPublicationContractTests(
    PublicationContract, HarrisCandidateContract, TransactionTestCase
):
    pass


class BrazosPublicationContractTests(
    PublicationContract, BrazosCandidateContract, TransactionTestCase
):
    pass
