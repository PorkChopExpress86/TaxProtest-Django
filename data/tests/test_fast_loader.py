"""Tests for the COPY-based fast loaders (PostgreSQL only)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from django.db import connection
from django.test import TestCase

from data.models import BuildingDetail, PropertyRecord


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "fast_loader uses PostgreSQL COPY; skipped on other backends",
)
class FastLoaderTests(TestCase):
    def _write(self, directory: str, name: str, lines: list[str]) -> Path:
        path = Path(directory) / name
        path.write_text("\n".join(lines) + "\n", encoding="latin-1")
        return path

    def test_copy_load_property_records_filters_and_loads(self) -> None:
        from data.etl_pipeline.fast_loader import copy_load_property_records

        with TemporaryDirectory() as d:
            path = self._write(
                d,
                "real_acct.txt",
                [
                    "acct\tmailto\tstr_num\tstr\tstr_sfx\tsite_addr_1\tsite_addr_2\tsite_addr_3\tstate_class\ttot_appr_val\tassessed_val\tbld_ar\tland_ar",
                    # residential, full address from components
                    "R1\tDOE JOHN\t100\tMAIN\tST\t\tHouston\t77001\tA1\t250000\t240000\t1800\t6000",
                    # residential, site_addr_1 wins over components
                    "R2\tSMITH JANE\t200\tELM\tAVE\t200 ELM AVE\tHouston\t77002\tA2\t\t\t\t",
                    # non-residential -> skipped
                    "C1\tACME LLC\t1\tCOMMERCE\tST\t\tHouston\t77003\tF1\t900000\t900000\t5000\t10000",
                    # blank account -> skipped
                    "\tNOBODY\t\t\t\t\t\t\tA1\t\t\t\t",
                ],
            )
            result = copy_load_property_records(path, truncate=True)

        self.assertEqual(result["loaded"], 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(PropertyRecord.objects.count(), 2)

        r1 = PropertyRecord.objects.get(account_number="R1")
        self.assertTrue(r1.is_residential)
        self.assertFalse(r1.is_data_ready)
        self.assertEqual(r1.address, "100 MAIN ST")
        self.assertEqual(r1.city, "Houston")
        self.assertEqual(r1.zipcode, "77001")
        self.assertEqual(float(r1.value), 250000.0)
        self.assertEqual(r1.parcel_id, "")
        self.assertEqual(r1.source_url, "")
        self.assertIsNotNone(r1.created_at)

        r2 = PropertyRecord.objects.get(account_number="R2")
        self.assertEqual(r2.address, "200 ELM AVE")
        self.assertIsNone(r2.value)

    def test_copy_load_building_details_links_and_uses_fixtures(self) -> None:
        from data.etl_pipeline.fast_loader import copy_load_building_details

        prop = PropertyRecord.objects.create(
            address="1 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="B1",
            state_class="A1",
            is_residential=True,
        )
        # Account with no PropertyRecord -> counted invalid.
        account_map = {"B1": prop.id}

        class _Fixtures:
            def get_bedroom_count(self, acct, bnum):
                return 4 if acct == "B1" else 0

            def get_bathroom_count(self, acct, bnum):
                return 2.5 if acct == "B1" else 0

            def get_fixtures(self, acct, bnum):
                return {"half_baths": 1 if acct == "B1" else 0}

        with TemporaryDirectory() as d:
            path = self._write(
                d,
                "building_res.txt",
                [
                    "acct\tbld_num\timprv_type\tbldg_class\tqa_cd\tcndtn_cd\tdate_erected\theat_ar\tsty\tbed_rm\tfull_bath\thalf_bath",
                    "B1\t1\tA1\tR3\tA\tG\t1995\t1800\t1\t\t\t",
                    "MISSING\t1\tA1\tR3\tA\tG\t2000\t1500\t1\t3\t2\t0",
                ],
            )
            result = copy_load_building_details(
                path, account_map=account_map, fixtures_aggregator=_Fixtures(), truncate=True
            )

        self.assertEqual(result["loaded"], 1)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(BuildingDetail.objects.count(), 1)

        b = BuildingDetail.objects.get(account_number="B1")
        self.assertEqual(b.property_id, prop.id)
        self.assertEqual(b.building_number, 1)
        self.assertEqual(b.year_built, 1995)
        self.assertEqual(float(b.heat_area), 1800.0)
        self.assertEqual(b.bedrooms, 4)
        self.assertEqual(float(b.bathrooms), 2.5)
        self.assertEqual(b.half_baths, 1)
        self.assertTrue(b.is_active)
        self.assertIsNotNone(b.created_at)
