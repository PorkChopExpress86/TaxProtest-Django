"""One Import operation: reservation, warnings, and classified status for every county import."""

import logging

from django.db import connection
from django.test import TransactionTestCase

from counties.common.county_registry import UnknownCounty
from counties.common.import_audit import OperationStatus, audited_operation
from counties.common.import_writers import WriterConflict, county_writer, fenced_write
from counties.common.models import ImportOperation
from counties.common.tests.fake_county import (
    FAKE_COUNTY,
    FAKE_LOCK_KEY,
    FAKE_LOGGER,
    registered_fake_county,
)


class ImportOperationTests(TransactionTestCase):
    def test_operation_holds_the_county_writer_until_it_ends(self):
        with audited_operation("brazos", "annual", actor="operator") as operation:
            competing = ImportOperation.objects.create(county="brazos", intent="annual")
            with self.assertRaises(WriterConflict), county_writer(competing):
                pass
        operation.refresh_from_db()
        self.assertEqual(operation.status, "completed")
        self.assertIsNotNone(operation.finished_at)
        with county_writer(competing):
            pass

    def test_caller_classification_is_recorded_with_captured_warnings(self):
        with audited_operation("harris", "full") as operation:
            logging.getLogger("etl_orchestrator").warning("Parcel file had duplicate rows")
            operation.status = OperationStatus.AWAITING_REVIEW
        operation.refresh_from_db()
        self.assertEqual(operation.status, "awaiting_review")
        self.assertEqual(operation.warnings, ["Parcel file had duplicate rows"])

    def test_brazos_warnings_are_captured_from_the_brazos_logger(self):
        with audited_operation("brazos", "annual") as operation:
            logging.getLogger("brazos_cad").warning("GIS archive missing a sidecar")
        operation.refresh_from_db()
        self.assertEqual(operation.warnings, ["GIS archive missing a sidecar"])

    def test_status_outside_the_vocabulary_is_rejected(self):
        with self.assertRaises(ValueError), audited_operation("harris", "full") as operation:
            operation.status = "validated"
        operation.refresh_from_db()
        self.assertEqual(operation.status, "failed")

    def test_failure_before_publication_fails_the_operation_and_is_raised(self):
        with self.assertRaises(OSError), audited_operation("harris", "full") as operation:
            logging.getLogger("etl_orchestrator").warning("Stage warning")
            raise OSError("Download interrupted")
        operation.refresh_from_db()
        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.errors, ["Download interrupted"])
        self.assertEqual(operation.warnings, ["Stage warning"])

    def test_rolled_back_publication_is_a_failure(self):
        with self.assertRaises(OSError), audited_operation("harris", "full") as operation:
            with fenced_write():
                operation.status = OperationStatus.PUBLISHED
                operation.publication_after = {"candidate_id": "rolled-back"}
                operation.save()
                raise OSError("Final audit failed")
        operation.refresh_from_db()
        self.assertEqual(operation.status, "failed")
        self.assertIsNone(operation.publication_after)
        self.assertEqual(operation.errors, ["Final audit failed"])

    def test_failure_after_publication_is_a_warning_and_not_raised(self):
        with audited_operation("brazos", "annual") as operation:
            with fenced_write():
                operation.status = OperationStatus.PUBLISHED
                operation.publication_after = {"snapshot_id": 7}
                operation.save()
            raise OSError("Extracted files could not be removed")
        operation.refresh_from_db()
        self.assertEqual(operation.status, "published")
        self.assertEqual(operation.publication_after, {"snapshot_id": 7})
        self.assertEqual(operation.errors, [])
        self.assertEqual(operation.warnings, ["Extracted files could not be removed"])
        self.assertEqual(
            operation.evidence["post_publication_failure"], "Extracted files could not be removed"
        )


class RegisteredCountyOperationTests(TransactionTestCase):
    """A fake third county runs an Import operation from its registration alone (ADR-0022)."""

    def test_an_unregistered_county_is_rejected_before_any_operation_is_recorded(self):
        with self.assertRaises(UnknownCounty), audited_operation(FAKE_COUNTY, "annual"):
            self.fail("An unregistered county must not run")
        self.assertFalse(ImportOperation.objects.exists())

    def test_a_fake_county_runs_under_its_own_lock_beside_a_real_county(self):
        with registered_fake_county():
            with audited_operation(FAKE_COUNTY, "annual") as operation:
                logging.getLogger(FAKE_LOGGER).warning("Travis appraisal roll is late")
                logging.getLogger("etl_orchestrator").warning("Not a Travis warning")
                with fenced_write():
                    operation.evidence["written"] = True
                with audited_operation("harris", "full"):
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT objid FROM pg_locks WHERE locktype = 'advisory' "
                            "AND classid = 0 AND objsubid = 1 AND granted "
                            "AND pid = pg_backend_pid()"
                        )
                        held = sorted(row[0] for row in cursor.fetchall())
        self.assertEqual(held, [742101, FAKE_LOCK_KEY])
        operation.refresh_from_db()
        self.assertEqual(operation.warnings, ["Travis appraisal roll is late"])
        self.assertTrue(operation.evidence["written"])
