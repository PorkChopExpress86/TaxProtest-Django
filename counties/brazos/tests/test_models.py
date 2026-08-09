"""Tests for the brazos_cad app — model roundtrip and constraint enforcement."""

from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from counties.brazos.models import (
    PropertyAccount,
    PropertyEntity,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)


class PropertyAccountModelTests(TestCase):
    def test_create_and_query_account(self):
        PropertyAccount.objects.create(
            prop_id="R12345",
            tax_year=2024,
            owner_name="Jane Doe",
            situs_address="123 Main St",
            total_value=Decimal("250000.00"),
            land_value=Decimal("60000.00"),
            improvement_value=Decimal("190000.00"),
            is_residential=True,
            state_class="A1",
        )
        self.assertEqual(PropertyAccount.objects.count(), 1)
        acct = PropertyAccount.objects.get(prop_id="R12345", tax_year=2024)
        self.assertEqual(acct.owner_name, "Jane Doe")
        self.assertEqual(acct.is_residential, True)
        self.assertEqual(str(acct.total_value), "250000.00")

    def test_unique_constraint_on_prop_id_and_tax_year(self):
        PropertyAccount.objects.create(prop_id="R12345", tax_year=2024, owner_name="First")
        with self.assertRaises(IntegrityError), transaction.atomic():
            PropertyAccount.objects.create(prop_id="R12345", tax_year=2024, owner_name="Duplicate")

    def test_same_account_different_year_allowed(self):
        PropertyAccount.objects.create(prop_id="R12345", tax_year=2024)
        PropertyAccount.objects.create(prop_id="R12345", tax_year=2025)
        self.assertEqual(PropertyAccount.objects.count(), 2)


class PropertyLandModelTests(TestCase):
    def test_create_and_query_land(self):
        PropertyLand.objects.create(
            prop_id="R12345",
            tax_year=2024,
            land_seq=1,
            land_use_code="R1",
            acreage=Decimal("0.2500"),
            land_value=Decimal("60000.00"),
        )
        self.assertEqual(PropertyLand.objects.count(), 1)
        row = PropertyLand.objects.get(prop_id="R12345", tax_year=2024, land_seq=1)
        self.assertEqual(row.land_use_code, "R1")


class PropertyImprovementModelTests(TestCase):
    def test_create_and_query_improvement(self):
        PropertyImprovement.objects.create(
            prop_id="R12345",
            imp_id="I00001",
            tax_year=2024,
            improvement_type="MA",
            improvement_value=Decimal("190000.00"),
            year_built=1998,
            square_feet=Decimal("2100.00"),
        )
        self.assertEqual(PropertyImprovement.objects.count(), 1)
        row = PropertyImprovement.objects.get(imp_id="I00001", tax_year=2024)
        self.assertEqual(row.year_built, 1998)

    def test_unique_constraint_on_prop_id_imp_id_and_tax_year(self):
        PropertyImprovement.objects.create(prop_id="R12345", imp_id="I00001", tax_year=2024)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PropertyImprovement.objects.create(prop_id="R12345", imp_id="I00001", tax_year=2024)

    def test_same_imp_id_different_prop_id_allowed(self):
        """Real-world collision case that broke the old (imp_id, tax_year)-only
        constraint: BCAD reuses the same imp_id across different prop_ids."""
        PropertyImprovement.objects.create(prop_id="R12345", imp_id="I00001", tax_year=2024)
        PropertyImprovement.objects.create(prop_id="R99999", imp_id="I00001", tax_year=2024)
        self.assertEqual(
            PropertyImprovement.objects.filter(imp_id="I00001", tax_year=2024).count(), 2
        )


class PropertyImprovementDetailModelTests(TestCase):
    def test_create_and_query_detail(self):
        PropertyImprovementDetail.objects.create(
            prop_id="R12345",
            imp_id="I00001",
            tax_year=2024,
            detail_seq=1,
            detail_description="Foundation",
            detail_value=Decimal("12000.00"),
        )
        self.assertEqual(PropertyImprovementDetail.objects.count(), 1)

    def test_detail_unit_accepts_long_free_text(self):
        long_text = "R30,U60,L30,D60" * 20  # well over the old 32-char CharField cap
        row = PropertyImprovementDetail.objects.create(
            prop_id="R12345",
            imp_id="I00001",
            tax_year=2024,
            detail_seq=1,
            detail_unit=long_text,
        )
        row.refresh_from_db()
        self.assertEqual(row.detail_unit, long_text)

    def test_unique_constraint_on_prop_id_imp_id_tax_year_detail_seq(self):
        PropertyImprovementDetail.objects.create(
            prop_id="R12345", imp_id="I00001", tax_year=2024, detail_seq=1
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            PropertyImprovementDetail.objects.create(
                prop_id="R12345", imp_id="I00001", tax_year=2024, detail_seq=1
            )

    def test_same_imp_id_detail_seq_different_prop_id_allowed(self):
        PropertyImprovementDetail.objects.create(
            prop_id="R12345", imp_id="I00001", tax_year=2024, detail_seq=1
        )
        PropertyImprovementDetail.objects.create(
            prop_id="R99999", imp_id="I00001", tax_year=2024, detail_seq=1
        )
        self.assertEqual(
            PropertyImprovementDetail.objects.filter(
                imp_id="I00001", tax_year=2024, detail_seq=1
            ).count(),
            2,
        )


class PropertyEntityModelTests(TestCase):
    def test_create_and_query_entity(self):
        PropertyEntity.objects.create(
            entity_id="E001",
            tax_year=2024,
            entity_code="C1",
        )
        self.assertEqual(PropertyEntity.objects.count(), 1)
        row = PropertyEntity.objects.get(entity_id="E001", tax_year=2024)
        self.assertEqual(row.entity_code, "C1")

    def test_unique_constraint_on_entity_id_and_tax_year(self):
        PropertyEntity.objects.create(entity_id="E001", tax_year=2024)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PropertyEntity.objects.create(entity_id="E001", tax_year=2024)
