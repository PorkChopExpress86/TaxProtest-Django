"""Test the county-neutral candidate staging module."""

from uuid import uuid4

import pytest
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount
from counties.common.candidate_staging import (
    compute_dataset_hash,
    cutover_staged_tables,
    staged_candidate_schema,
    switch_search_path,
    validate_schema_name,
)
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

        with staged_candidate_schema(
            "brazos",
            candidate,
            models,
            shared_models_scope=shared_scope,
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
