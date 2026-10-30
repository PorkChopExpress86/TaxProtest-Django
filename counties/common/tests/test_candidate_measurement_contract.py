"""The Candidate lifecycle measures each dataset state once and never reuses a stale value."""

from contextlib import contextmanager
from unittest.mock import patch

from django.test import TransactionTestCase

from counties.common import candidate_lifecycle, candidate_ports
from counties.common.candidate_lifecycle import prepare, publish
from counties.common.candidate_ports import dataset_identity, published_identity
from counties.common.county_registry import registration_for
from counties.common.import_audit import audited_operation
from counties.common.import_review import ImportReviewRejected, Measurement
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)

PUBLISHED = ("identity", "public")
PUBLISHED_OUTCOMES = ("outcomes", "published")
CANDIDATE_OUTCOMES = ("outcomes", "candidate")


class MeasurementContract:
    """Expensive measurements every county port gets from the shared lifecycle."""

    @contextmanager
    def measurements(self):
        """Log each content digest, outcome survey and cutover, in the order they run."""
        log = []
        digest = candidate_ports.compute_dataset_hash
        cutover = candidate_lifecycle.cutover_staged_tables
        port = type(registration_for(self.county).port)
        outcomes = port.outcomes

        def logged_digest(models, *, schema="public", **kwargs):
            log.append(("identity", schema))
            return digest(models, schema=schema, **kwargs)

        def logged_cutover(*args, **kwargs):
            log.append("cutover")
            return cutover(*args, **kwargs)

        def logged_outcomes(self, candidate):
            log.append(PUBLISHED_OUTCOMES if candidate is None else CANDIDATE_OUTCOMES)
            return outcomes(self, candidate)

        with (
            patch.object(candidate_ports, "compute_dataset_hash", logged_digest),
            patch.object(candidate_lifecycle, "cutover_staged_tables", logged_cutover),
            patch.object(port, "outcomes", logged_outcomes),
        ):
            yield log

    def publish_reviewed(self):
        with audited_operation(self.county, "apply", actor="operator") as operation:
            publish(operation, self.candidate.pk, user=self.reviewer, reason="Apply reviewed")
        return operation

    def test_an_automatic_publication_measures_each_dataset_state_once(self):
        self.approve()
        self.publish_reviewed()
        self.write_candidate_rows(ready=True)
        with (
            self.measurements() as log,
            audited_operation(self.county, "fixture", actor="operator") as operation,
        ):
            prepared = prepare(operation, self.fixture_load(operation), automatic_publication=True)
        candidate = prepared.candidate
        self.assertEqual(candidate.state, "published")
        self.assertEqual(
            log,
            [
                PUBLISHED,
                PUBLISHED_OUTCOMES,
                CANDIDATE_OUTCOMES,
                ("identity", candidate.storage_schema),
                "cutover",
                PUBLISHED,
            ],
        )
        operation.refresh_from_db()
        # The after-cutover identity was read again, inside the cutover transaction.
        self.assertNotEqual(operation.publication_after, operation.publication_before)
        self.assertEqual(
            operation.publication_after,
            published_identity(self.county) | {"candidate_id": str(candidate.pk)},
        )

    def test_each_review_path_operation_measures_each_dataset_state_once(self):
        staged = ("identity", self.candidate.storage_schema)
        with self.measurements() as log:
            self.approve()
        self.assertEqual(log, [PUBLISHED, staged, PUBLISHED_OUTCOMES, CANDIDATE_OUTCOMES])
        with self.measurements() as log:
            self.publish_reviewed()
        self.assertEqual(
            log,
            [PUBLISHED, staged, PUBLISHED_OUTCOMES, CANDIDATE_OUTCOMES, "cutover", PUBLISHED],
        )

    def test_published_data_changed_after_review_is_measured_again(self):
        self.approve()
        self.add_published_record("CHANGED")
        with self.assertRaisesMessage(ImportReviewRejected, "Published baseline changed"):
            self.publish_reviewed()
        self.assertEqual(self.published_keys(), ["CHANGED", "PUBLISHED"])

    def test_a_separate_publication_in_the_preparing_operation_measures_again(self):
        self.approve()
        self.publish_reviewed()
        self.write_candidate_rows(ready=True)
        with (
            self.assertRaisesMessage(ImportReviewRejected, "Published baseline changed"),
            audited_operation(self.county, "fixture", actor="operator") as operation,
        ):
            candidate = prepare(operation, self.fixture_load(operation)).candidate
            self.assertEqual(candidate.state, "prepared")
            before = dataset_identity(self.county)
            self.add_published_record("CHANGED")
            self.assertNotEqual(dataset_identity(self.county), before)
            publish(operation, candidate.pk, reason="Publish after a change")

    def test_a_measurement_qualifies_only_its_candidate_in_its_operation(self):
        self.approve()
        self.publish_reviewed()
        self.write_candidate_rows(ready=True)
        published = self.published_keys()
        with audited_operation(self.county, "fixture", actor="operator") as operation:
            candidate = prepare(operation, self.fixture_load(operation)).candidate
        measured = Measurement(candidate.pk, None, {}, {}, {})
        with (
            self.assertRaisesMessage(ValueError, "Only the operation that measured"),
            audited_operation(self.county, "apply", actor="operator") as other,
        ):
            publish(other, candidate.pk, measured=measured)
        with (
            self.assertRaisesMessage(ValueError, "another candidate"),
            audited_operation(self.county, "fixture", actor="operator") as operation,
        ):
            prepared = prepare(operation, self.fixture_load(operation)).candidate
            publish(operation, prepared.pk, measured=measured)
        self.assertEqual(self.published_keys(), published)


class HarrisMeasurementContractTests(
    MeasurementContract, HarrisCandidateContract, TransactionTestCase
):
    pass


class BrazosMeasurementContractTests(
    MeasurementContract, BrazosCandidateContract, TransactionTestCase
):
    pass
