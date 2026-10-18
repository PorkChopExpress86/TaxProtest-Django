"""Test the county-neutral candidate staging module."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection, models, transaction
from django.test import SimpleTestCase, TransactionTestCase

from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount
from counties.common.candidate_staging import (
    _CopiedRows,
    compute_dataset_hash,
    cutover_staged_tables,
    staged_candidate_schema,
    validate_schema_name,
)
from counties.common.import_writers import county_writer
from counties.common.models import ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption


class CandidateStagingUnitTests(SimpleTestCase):
    def test_validate_schema_name_accepts_valid_schemas(self):
        valid = f"brazos_candidate_{uuid4().hex}"
        validate_schema_name(valid, county="brazos")
        validate_schema_name(f"harris_candidate_{uuid4().hex}", county="harris")

    def test_validate_schema_name_rejects_invalid_schemas(self):
        with pytest.raises(ValueError, match="Invalid candidate storage identity"):
            validate_schema_name("not_a_valid_schema")

        with pytest.raises(ValueError, match="does not match county"):
            validate_schema_name(f"brazos_candidate_{uuid4().hex}", county="harris")

    def test_copied_rows_hash_row_text_and_refuse_other_copy_escapes(self):
        digest = hashlib.sha256()
        _CopiedRows(digest).write(b'{"owner":"a \\\\\\\\ b"}\n')
        self.assertEqual(digest.hexdigest(), hashlib.sha256(b'{"owner":"a \\\\ b"}\n').hexdigest())
        # A json column keeps its raw newline, which COPY writes as backslash-n.
        with pytest.raises(ValueError, match="COPY escaped a dataset row"):
            _CopiedRows(hashlib.sha256()).write(b'{"js":{"a":\\n1}}\n')


def keyset_dataset_hash(models_list, scope_filters):
    """The 1000-row keyset scan that made every dataset identity persisted before #144."""
    digest = hashlib.sha256()
    with connection.cursor() as cursor:
        for model in models_list:
            table = connection.ops.quote_name(model._meta.db_table)
            scope = scope_filters.get(model, "")
            digest.update(table.encode())
            last_id = 0
            while True:
                cursor.execute(
                    f'SELECT id, row_to_json(t)::text FROM "public".{table} t '
                    f"WHERE id > %s {scope} ORDER BY id LIMIT 1000",
                    [last_id],
                )
                rows = cursor.fetchall()
                if not rows:
                    break
                for row_id, record in rows:
                    digest.update(record.encode())
                    digest.update(b"\n")
                    last_id = row_id
    return {"sha256": digest.hexdigest()}


class DatasetHashContractTests(TransactionTestCase):
    """Persisted dataset identities are a byte-exact contract (ADR-0024)."""

    def setUp(self):
        owners = [
            'O\'Brien "Bud" \\ Jr. \\\\N',
            "Línea\nnueva\ttab\r\b\f\v\x01\x1f\x7f",
            "日本 🏠",
            "",
        ]
        PropertyAccount.objects.bulk_create(
            PropertyAccount(
                id=account_id,
                prop_id=f"{account_id:012d}",
                tax_year=2026,
                owner_name=owners[account_id % len(owners)],
                total_value=Decimal("1234.50") if account_id % 3 else None,
                latitude=Decimal("30.6279830"),
            )
            # Ids out of insertion order, spanning several 1000-row keyset pages; 0 is unscanned.
            for account_id in [*range(2600, 100, -1), 7, 3, 0]
        )
        PropertyAccount.objects.filter(id=1500).delete()
        PropertyJurisdictionExemption.objects.bulk_create(
            PropertyJurisdictionExemption(
                id=exemption_id,
                county="harris" if exemption_id % 2 else "brazos",
                tax_year=2026,
                account_number=f"{exemption_id:012d}",
                tax_unit_code="GBC",
                exemption_amount=Decimal("25000.00"),
            )
            for exemption_id in range(1, 40)
        )
        for model in (PropertyAccount, PropertyJurisdictionExemption):
            model.objects.update(
                **{
                    field.name: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
                    for field in model._meta.fields
                    if isinstance(field, models.DateTimeField)
                }
            )

    def test_streamed_digest_matches_the_keyset_scan_and_its_pinned_bytes(self):
        tables = (PropertyAccount, PropertyJurisdictionExemption)
        scopes = {PropertyJurisdictionExemption: "AND county = 'brazos'"}
        expected = {"sha256": "7b80ef8affb970b1fef4d329f2a52268dfcd986e790d48d320ecae7a96cf3618"}

        self.assertEqual(keyset_dataset_hash(tables, scopes), expected)
        self.assertEqual(compute_dataset_hash(tables, scope_filters=scopes), expected)
        with transaction.atomic():
            PropertyAccount.objects.filter(id=7).update(owner_name="Uncommitted \\ owner")
            changed = keyset_dataset_hash(tables, scopes)
            self.assertNotEqual(changed, expected)
            self.assertEqual(compute_dataset_hash(tables, scope_filters=scopes), changed)


class CandidateStagingIntegrationTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.filter(county="brazos"):
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def test_compute_dataset_hash_is_deterministic(self):
        models = (PropertyAccount, BrazosPropertySnapshot)
        hash1 = compute_dataset_hash(models)
        hash2 = compute_dataset_hash(models)
        self.assertEqual(hash1, hash2)
        self.assertIn("sha256", hash1)

    def test_staged_candidate_schema_and_cutover(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL")

        models = (PropertyAccount, PropertyJurisdictionExemption, BrazosPropertySnapshot)
        shared_scope = {PropertyJurisdictionExemption: " WHERE county = 'brazos'"}
        shared_county = {PropertyJurisdictionExemption: "brazos"}

        operation = ImportOperation.objects.create(
            county="brazos",
            intent="annual",
            requested_year=2026,
            origin="operator",
        )
        candidate = ImportCandidate.objects.create(
            county="brazos",
            operation=operation,
            storage_schema=f"brazos_candidate_{uuid4().hex}",
            baseline={},
            request={"mode": "annual", "tax_year": 2026},
            evidence={},
        )

        # Baseline in public schema
        PropertyAccount.objects.create(
            tax_year=2026, prop_id="000000010001", owner_name="Original Owner"
        )
        PropertyJurisdictionExemption.objects.create(
            county="brazos",
            tax_year=2026,
            account_number="000000010001",
            tax_unit_code="GBC",
            exemption_code="HS",
        )
        PropertyJurisdictionExemption.objects.create(
            county="harris",
            tax_year=2026,
            account_number="999999999999",
            tax_unit_code="GHC",
            exemption_code="HS",
        )

        with (
            county_writer(operation),
            staged_candidate_schema(
                "brazos",
                candidate,
                models,
                shared_models_scope=shared_scope,
            ),
        ):
            # Inside staged schema, update data
            staged_account = PropertyAccount.objects.get(prop_id="000000010001")
            staged_account.owner_name = "New Candidate Owner"
            staged_account.save()

            PropertyAccount.objects.create(
                tax_year=2026, prop_id="000000010002", owner_name="Second Owner"
            )

        # In public schema, original data remains unchanged
        self.assertEqual(
            PropertyAccount.objects.get(prop_id="000000010001").owner_name, "Original Owner"
        )
        self.assertFalse(PropertyAccount.objects.filter(prop_id="000000010002").exists())

        # Now cutover
        cutover_staged_tables(
            candidate,
            models,
            shared_models_scope=shared_scope,
            shared_models_county=shared_county,
        )

        # In public schema, data now reflects the candidate
        self.assertEqual(
            PropertyAccount.objects.get(prop_id="000000010001").owner_name,
            "New Candidate Owner",
        )
        self.assertTrue(PropertyAccount.objects.filter(prop_id="000000010002").exists())

        # Harris row in shared table was preserved
        self.assertTrue(
            PropertyJurisdictionExemption.objects.filter(
                county="harris", account_number="999999999999"
            ).exists()
        )
