"""Publication exchanges county-owned tables and replaces rows of the rest (ADR-0025)."""

import threading
import time
from unittest.mock import patch

from django.db import DatabaseError, close_old_connections, connection, connections
from django.http import HttpResponse
from django.test import RequestFactory, TransactionTestCase

from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount
from counties.common import candidate_staging
from counties.common.candidate_lifecycle import apply
from counties.common.candidate_ports import dataset_identity, published_identity
from counties.common.candidate_staging import CandidateDefinitionMismatch
from counties.common.models import ImportCandidate
from counties.common.tax_models import PropertyJurisdictionExemption
from counties.common.tests.candidate_contract import (
    BrazosCandidateContract,
    HarrisCandidateContract,
)
from counties.common.tests.test_candidate_publication_contract import PublicationContract
from counties.common.views import consistent_published_read
from counties.harris.etl_pipeline import run_harris_import
from counties.harris.etl_pipeline.tests.test_import_publication import harris_request
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord

HARRIS_TABLES = (PropertyRecord, BuildingDetail, ExtraFeature)


def table_oid(model) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s::regclass::oid", [f"public.{model._meta.db_table}"])
        return cursor.fetchone()[0]


def named_objects(schema: str, model) -> dict[str, list[str]]:
    """The names of a table's indexes, constraints and owned sequences in ``schema``."""
    table = f'"{schema}"."{model._meta.db_table}"'
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid = %s::regclass",
            [table],
        )
        indexes = sorted(name.rsplit(".", 1)[-1] for (name,) in cursor.fetchall())
        cursor.execute("SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass", [table])
        constraints = sorted(name for (name,) in cursor.fetchall())
        cursor.execute(
            "SELECT s.relname FROM pg_depend d JOIN pg_class s ON s.oid = d.objid "
            "WHERE d.refobjid = %s::regclass AND s.relkind = 'S'",
            [table],
        )
        sequences = sorted(name for (name,) in cursor.fetchall())
    return {"indexes": indexes, "constraints": constraints, "sequences": sequences}


def foreign_key_targets(model) -> dict[str, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname, confrelid::oid FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid = %s::regclass",
            [f"public.{model._meta.db_table}"],
        )
        return dict(cursor.fetchall())


class Publishes:
    publish = PublicationContract.publish


class HarrisExchangeTests(Publishes, HarrisCandidateContract, TransactionTestCase):
    def test_staged_tables_carry_production_names(self):
        for model in HARRIS_TABLES:
            with self.subTest(table=model._meta.db_table):
                self.assertEqual(
                    named_objects(self.candidate.storage_schema, model),
                    named_objects("public", model),
                )

    def test_publication_exchanges_every_harris_table(self):
        before = {model: table_oid(model) for model in HARRIS_TABLES}
        names = {model: named_objects("public", model) for model in HARRIS_TABLES}
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{self.candidate.storage_schema}", public')
            staged = dataset_identity("harris", self.candidate.storage_schema)
            staged_rows = list(
                PropertyRecord.objects.order_by("id").values_list("id", "account_number")
            )
            cursor.execute("SET search_path TO public")
        self.approve()

        self.publish()

        self.assertEqual(dataset_identity("harris"), staged)
        self.assertEqual(
            list(PropertyRecord.objects.order_by("id").values_list("id", "account_number")),
            staged_rows,
        )
        for model in HARRIS_TABLES:
            with self.subTest(table=model._meta.db_table):
                self.assertNotEqual(table_oid(model), before[model])
                self.assertEqual(named_objects("public", model), names[model])
        new_property = table_oid(PropertyRecord)
        for model in (BuildingDetail, ExtraFeature):
            self.assertEqual(set(foreign_key_targets(model).values()), {new_property})
        # The replaced tables went with the candidate schema.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_namespace WHERE nspname LIKE %s",
                [f"{self.candidate.storage_schema}%"],
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_migrations_find_exchanged_objects_by_name(self):
        self.approve()
        self.publish()

        (index,) = BuildingDetail._meta.indexes
        with connection.schema_editor() as editor:
            editor.remove_index(BuildingDetail, index)
            editor.add_index(BuildingDetail, index)
        constraint = BuildingDetail._meta.constraints[0]
        with connection.schema_editor() as editor:
            editor.remove_constraint(BuildingDetail, constraint)
            editor.add_constraint(BuildingDetail, constraint)

    def test_failure_after_the_exchange_keeps_the_previous_tables(self):
        before = {model: table_oid(model) for model in HARRIS_TABLES}
        identity = published_identity("harris")
        self.approve()

        def fail(execute, sql, params, many, context):
            if sql.startswith('INSERT INTO "data_importauditentry"'):
                raise OSError("Final publication audit failed")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail), self.assertRaises(OSError):
            self.publish()
        self.assertEqual({model: table_oid(model) for model in HARRIS_TABLES}, before)
        self.assertEqual(published_identity("harris"), identity)

    def test_a_held_read_lock_times_out_publication_without_changing_it(self):
        identity = published_identity("harris")
        self.approve()
        reader = connections.create_connection("default")
        try:
            with reader.cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute("SELECT count(*) FROM data_propertyrecord")
            with (
                patch.object(candidate_staging, "EXCHANGE_LOCK_TIMEOUT", "200ms"),
                self.assertRaisesMessage(DatabaseError, "could not lock the published tables"),
            ):
                self.publish()
        finally:
            reader.close()
        self.assertEqual(published_identity("harris"), identity)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.state, "approved")

    def test_a_timed_out_automatic_publication_leaves_its_candidate_publishable(self):
        self.approve()
        self.publish()
        reader = connections.create_connection("default")
        try:
            with reader.cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute("SELECT count(*) FROM data_propertyrecord")
            with (
                patch.object(candidate_staging, "EXCHANGE_LOCK_TIMEOUT", "100ms"),
                patch.object(candidate_staging, "EXCHANGE_LOCK_ATTEMPTS", 1),
                self.assertRaisesMessage(DatabaseError, "could not lock the published tables"),
            ):
                run_harris_import(harris_request(self.root))
        finally:
            reader.close()

        held = ImportCandidate.objects.exclude(pk=self.candidate.pk).get()
        self.assertEqual(held.state, "prepared")
        applied = apply(held, user=self.reviewer, reason="Apply after shared reads finished")
        self.assertEqual(applied.status, "published")

    def test_an_open_reader_sees_the_new_dataset_after_publication(self):
        self.approve()
        reader = connections.create_connection("default")
        try:
            with reader.cursor() as cursor:
                cursor.execute("SELECT account_number FROM data_propertyrecord ORDER BY 1")
                self.assertEqual(cursor.fetchall(), [("PUBLISHED",)])
                self.publish()
                cursor.execute("SELECT account_number FROM data_propertyrecord ORDER BY 1")
                self.assertEqual([key for (key,) in cursor.fetchall()], self.published_keys())
        finally:
            reader.close()
        self.assertNotEqual(self.published_keys(), ["PUBLISHED"])

    def test_a_shared_response_started_before_publication_reads_only_the_old_dataset(self):
        self.approve()
        entered, resume = threading.Event(), threading.Event()
        seen = []

        @consistent_published_read
        def view(request):
            # Its first read touches no exchanged table, so no table lock protects it.
            seen.append(ImportCandidate.objects.count())
            entered.set()
            resume.wait(10)
            seen.append(sorted(PropertyRecord.objects.values_list("account_number", flat=True)))
            return HttpResponse()

        def read():
            close_old_connections()
            try:
                view(RequestFactory().get("/"))
            finally:
                connection.close()

        reader = threading.Thread(target=read)
        reader.start()
        self.assertTrue(entered.wait(5))
        threading.Timer(1, resume.set).start()
        started = time.monotonic()
        self.publish()
        reader.join(10)

        self.assertGreaterEqual(time.monotonic() - started, 1)
        self.assertEqual(seen[1], ["PUBLISHED"])
        self.assertEqual(self.candidate.state, "published")

    def test_publication_refuses_a_candidate_staged_before_a_schema_change(self):
        identity = published_identity("harris")
        self.approve()
        with connection.cursor() as cursor:
            cursor.execute("CREATE INDEX exchange_test_idx ON data_extrafeature (feature_code)")
        try:
            with self.assertRaisesMessage(CandidateDefinitionMismatch, "data_extrafeature"):
                self.publish()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP INDEX exchange_test_idx")
        self.assertEqual(published_identity("harris"), identity)


class BrazosExchangeTests(Publishes, BrazosCandidateContract, TransactionTestCase):
    def test_referenced_and_shared_tables_keep_row_replacement(self):
        kept = (BrazosPropertySnapshot, PropertyJurisdictionExemption)
        before = {model: table_oid(model) for model in (PropertyAccount, *kept)}
        PropertyJurisdictionExemption.objects.create(
            county="harris", tax_year=2026, account_number="999999999999", tax_unit_code="GHC"
        )
        self.approve()

        self.publish()

        # A coordinate enrichment audit references the snapshot table from outside the set.
        self.assertNotEqual(table_oid(PropertyAccount), before[PropertyAccount])
        for model in kept:
            self.assertEqual(table_oid(model), before[model])
        self.assertTrue(
            PropertyJurisdictionExemption.objects.filter(
                county="harris", account_number="999999999999"
            ).exists()
        )
