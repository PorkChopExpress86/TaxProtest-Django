"""Parity tests: COPY and ORM paths must produce identical database rows.

Runs the same fixture file through ``copy_load`` (PostgreSQL COPY) and
``model_loader.bulk_load`` (Django ORM ``bulk_create``) and asserts that
both paths produce the same row count and the same field values.

This locks the equivalence of the two loading paths so a future change
to one can't silently diverge from the other — the core risk that
motivated unifying them onto a shared ``row_reader`` in the first place.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from django.db import connection
from django.test import TransactionTestCase

from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "Parity test requires both COPY and ORM paths on PostgreSQL",
)
class PropertyRecordParityTests(TransactionTestCase):
    """COPY and ORM paths must produce identical PropertyRecord rows."""

    FIXTURE_LINES = [
        "acct\tmailto\tstr_num\tstr\tstr_sfx\tsite_addr_1\tsite_addr_2\tsite_addr_3\tstate_class\ttot_appr_val\tassessed_val\tbld_ar\tland_ar",
        "P1\tDOE JOHN\t100\tMAIN\tST\t\tHouston\t77001\tA1\t250000\t240000\t1800\t6000",
        "P2\tSMITH JANE\t200\tELM\tAVE\t200 ELM AVE\tHouston\t77002\tA2\t350000\t345000\t2200\t8000",
        "P3\tBROWN BOB\t300\tOAK\tLN\t\tSpring\t77373\tA4\t425000\t420000\t2500\t10000",
        "C1\tACME LLC\t1\tCOMMERCE\tST\t\tHouston\t77003\tF1\t900000\t900000\t5000\t10000",
        "\tNOBODY\t\t\t\t\t\t\tA1\t\t\t\t",
    ]

    DB_COLUMNS = [
        "account_number",
        "address",
        "city",
        "zipcode",
        "owner_name",
        "value",
        "assessed_value",
        "building_area",
        "land_area",
        "state_class",
        "is_residential",
        "is_data_ready",
        "street_number",
        "street_name",
        "source_url",
        "parcel_id",
    ]

    ORM_FIELD_NAMES = [
        "account_number",
        "address",
        "city",
        "zipcode",
        "owner_name",
        "value",
        "assessed_value",
        "building_area",
        "land_area",
        "state_class",
        "is_residential",
        "is_data_ready",
        "street_number",
        "street_name",
        "source_url",
        "parcel_id",
    ]

    def _write_fixture(self, d: str) -> Path:
        path = Path(d) / "real_acct.txt"
        path.write_text("\n".join(self.FIXTURE_LINES) + "\n", encoding="latin-1")
        return path

    def test_property_record_copy_matches_orm(self):
        from django.utils import timezone

        from counties.harris.etl_pipeline.config import ETLConfig
        from counties.harris.etl_pipeline.fast_loader import copy_load
        from counties.harris.etl_pipeline.model_loader import ModelLoader
        from counties.harris.etl_pipeline.row_reader import iter_property_rows

        now_iso = timezone.now().isoformat()

        # COPY path
        with TemporaryDirectory() as d:
            path = self._write_fixture(d)
            copy_result = copy_load(
                table="data_propertyrecord",
                columns=self.DB_COLUMNS,
                row_gen=iter_property_rows(path),
                truncate=True,
                extra_columns={"created_at": now_iso, "updated_at": now_iso},
            )
        copy_rows = {p.account_number: p for p in PropertyRecord.objects.all()}

        # ORM path (truncate first, then reload)
        config = ETLConfig(
            download_dir=Path("/tmp/_parity_d"),
            extract_dir=Path("/tmp/_parity_e"),
            log_dir=Path("/tmp/_parity_l"),
        )
        loader = ModelLoader(config, batch_size=10)
        with TemporaryDirectory() as d:
            path = self._write_fixture(d)
            orm_result = loader.bulk_load(
                model_class=PropertyRecord,
                row_gen=iter_property_rows(path),
                field_names=self.ORM_FIELD_NAMES,
                truncate=True,
            )
        orm_rows = {p.account_number: p for p in PropertyRecord.objects.all()}

        # Assert identical results
        self.assertEqual(copy_result["loaded"], orm_result.records_loaded)
        self.assertEqual(copy_result["skipped"], orm_result.records_skipped)
        self.assertEqual(len(copy_rows), len(orm_rows))
        self.assertEqual(set(copy_rows.keys()), set(orm_rows.keys()))

        for acct in copy_rows:
            c = copy_rows[acct]
            o = orm_rows[acct]
            self.assertEqual(c.address, o.address, f"address mismatch for {acct}")
            self.assertEqual(c.city, o.city, f"city mismatch for {acct}")
            self.assertEqual(c.zipcode, o.zipcode, f"zipcode mismatch for {acct}")
            self.assertEqual(c.owner_name, o.owner_name, f"owner_name mismatch for {acct}")
            self.assertEqual(c.state_class, o.state_class, f"state_class mismatch for {acct}")
            self.assertEqual(
                c.is_residential, o.is_residential, f"is_residential mismatch for {acct}"
            )
            self.assertEqual(c.is_data_ready, o.is_data_ready, f"is_data_ready mismatch for {acct}")
            self.assertEqual(c.street_number, o.street_number, f"street_number mismatch for {acct}")
            self.assertEqual(c.street_name, o.street_name, f"street_name mismatch for {acct}")
            self.assertEqual(c.source_url, o.source_url, f"source_url mismatch for {acct}")
            self.assertEqual(c.parcel_id, o.parcel_id, f"parcel_id mismatch for {acct}")
            # Decimal fields — compare as float for tolerance
            for field in ["value", "assessed_value", "building_area", "land_area"]:
                cv = getattr(c, field)
                ov = getattr(o, field)
                if cv is None or ov is None:
                    self.assertEqual(cv, ov, f"{field} mismatch for {acct}: {cv} vs {ov}")
                else:
                    self.assertAlmostEqual(
                        float(cv),
                        float(ov),
                        places=2,
                        msg=f"{field} mismatch for {acct}: {cv} vs {ov}",
                    )


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "Parity test requires both COPY and ORM paths on PostgreSQL",
)
class BuildingDetailParityTests(TransactionTestCase):
    """COPY and ORM paths must produce identical BuildingDetail rows."""

    FIXTURE_LINES = [
        "acct\tbld_num\timprv_type\tbldg_class\tqa_cd\tcndtn_cd\tdate_erected\theat_ar\tsty\tbed_rm\tfull_bath\thalf_bath",
        "B1\t1\tA1\tR3\tA\tG\t1995\t1800\t1\t\t\t",
        "B1\t2\tA1\tR3\tA\tG\t1995\t1200\t1\t3\t2\t1",
        "MISSING\t1\tA1\tR3\tA\tG\t2000\t1500\t1\t3\t2\t0",
    ]

    DB_COLUMNS = [
        "property_id",
        "account_number",
        "building_number",
        "building_type",
        "building_style",
        "building_class",
        "quality_code",
        "condition_code",
        "year_built",
        "year_remodeled",
        "effective_year",
        "heat_area",
        "base_area",
        "gross_area",
        "stories",
        "foundation_type",
        "exterior_wall",
        "roof_cover",
        "roof_type",
        "bedrooms",
        "bathrooms",
        "half_baths",
        "fireplaces",
        "is_active",
    ]

    ORM_FIELD_NAMES = list(DB_COLUMNS)

    class _Fixtures:
        def get_bedroom_count(self, acct, bnum):
            return 0

        def get_bathroom_count(self, acct, bnum):
            return 0

        def get_fixtures(self, acct, bnum):
            return {"half_baths": 0}

    def test_building_detail_copy_matches_orm(self):
        from django.utils import timezone

        from counties.harris.etl_pipeline.config import ETLConfig
        from counties.harris.etl_pipeline.fast_loader import copy_load
        from counties.harris.etl_pipeline.model_loader import ModelLoader
        from counties.harris.etl_pipeline.row_reader import iter_building_rows

        # Create a property for account B1
        prop = PropertyRecord.objects.create(
            address="1 MAIN ST",
            city="Houston",
            zipcode="77001",
            account_number="B1",
            state_class="A1",
            is_residential=True,
        )
        account_map = {"B1": prop.id}
        fixtures = self._Fixtures()

        now_iso = timezone.now().isoformat()
        batch_id = timezone.now().strftime("%Y%m%d_%H%M%S")

        # COPY path
        with TemporaryDirectory() as d:
            path = Path(d) / "building_res.txt"
            path.write_text("\n".join(self.FIXTURE_LINES) + "\n", encoding="latin-1")
            copy_result = copy_load(
                table="data_buildingdetail",
                columns=self.DB_COLUMNS,
                row_gen=iter_building_rows(
                    path, account_map=account_map, fixtures_aggregator=fixtures
                ),
                truncate=True,
                extra_columns={
                    "import_date": now_iso,
                    "import_batch_id": batch_id,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                },
            )
        copy_rows = {(b.account_number, b.building_number): b for b in BuildingDetail.objects.all()}

        # ORM path
        config = ETLConfig(
            download_dir=Path("/tmp/_parity_bd"),
            extract_dir=Path("/tmp/_parity_be"),
            log_dir=Path("/tmp/_parity_bl"),
        )
        loader = ModelLoader(config, batch_size=10)
        with TemporaryDirectory() as d:
            path = Path(d) / "building_res.txt"
            path.write_text("\n".join(self.FIXTURE_LINES) + "\n", encoding="latin-1")
            orm_result = loader.bulk_load(
                model_class=BuildingDetail,
                row_gen=iter_building_rows(
                    path, account_map=account_map, fixtures_aggregator=fixtures
                ),
                field_names=self.ORM_FIELD_NAMES,
                truncate=True,
                extra_fields={
                    "import_date": timezone.now(),
                    "import_batch_id": batch_id,
                },
            )
        orm_rows = {(b.account_number, b.building_number): b for b in BuildingDetail.objects.all()}

        # Assert identical results
        self.assertEqual(copy_result["loaded"], orm_result.records_loaded)
        self.assertEqual(copy_result["invalid"], orm_result.records_invalid)
        self.assertEqual(copy_result["skipped"], orm_result.records_skipped)
        self.assertEqual(len(copy_rows), len(orm_rows))
        self.assertEqual(set(copy_rows.keys()), set(orm_rows.keys()))

        for key in copy_rows:
            c = copy_rows[key]
            o = orm_rows[key]
            self.assertEqual(c.property_id, o.property_id, f"property_id mismatch for {key}")
            self.assertEqual(
                c.building_number, o.building_number, f"building_number mismatch for {key}"
            )
            self.assertEqual(c.building_type, o.building_type, f"building_type mismatch for {key}")
            self.assertEqual(c.quality_code, o.quality_code, f"quality_code mismatch for {key}")
            self.assertEqual(
                c.condition_code, o.condition_code, f"condition_code mismatch for {key}"
            )
            self.assertEqual(c.year_built, o.year_built, f"year_built mismatch for {key}")
            self.assertEqual(c.bedrooms, o.bedrooms, f"bedrooms mismatch for {key}")
            self.assertEqual(c.half_baths, o.half_baths, f"half_baths mismatch for {key}")
            self.assertEqual(c.is_active, o.is_active, f"is_active mismatch for {key}")
            for field in ["heat_area", "bathrooms", "stories"]:
                cv = getattr(c, field)
                ov = getattr(o, field)
                if cv is None or ov is None:
                    self.assertEqual(cv, ov, f"{field} mismatch for {key}: {cv} vs {ov}")
                else:
                    self.assertAlmostEqual(
                        float(cv),
                        float(ov),
                        places=2,
                        msg=f"{field} mismatch for {key}: {cv} vs {ov}",
                    )


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "Parity test requires both COPY and ORM paths on PostgreSQL",
)
class ExtraFeatureParityTests(TransactionTestCase):
    """COPY and ORM paths must produce identical ExtraFeature rows."""

    FIXTURE_LINES = [
        "acct\tbld_num\tcd\tl_dscr\tcount\tarea\tlength\twidth\tgrade\tcond_cd\tact_yr\tuts",
        "E1\t1\tRS1\tFrame Utility Shed\t1\t150\t15\t10\tC\tAV\t2010\t2500",
        "E1\t2\tSPL\tGunite Pool\t1\t450\t30\t15\tB\tGD\t2015\t35000",
        "E1\t2\tSPL\tGunite Pool Duplicate\t1\t450\t30\t15\tB\tGD\t2015\t35000",  # Duplicate key: dropped by ON CONFLICT / ignore_conflicts
        "MISSING\t1\tRS1\tShed on missing account\t1\t100\t10\t10\tC\tAV\t2012\t1000",
    ]

    DB_COLUMNS = [
        "property_id",
        "account_number",
        "feature_number",
        "feature_code",
        "feature_description",
        "quantity",
        "area",
        "length",
        "width",
        "quality_code",
        "condition_code",
        "year_built",
        "value",
        "is_active",
    ]

    ORM_FIELD_NAMES = list(DB_COLUMNS)

    def test_extra_feature_copy_matches_orm(self):
        from django.utils import timezone

        from counties.harris.etl_pipeline.config import ETLConfig
        from counties.harris.etl_pipeline.fast_loader import copy_load
        from counties.harris.etl_pipeline.model_loader import ModelLoader
        from counties.harris.etl_pipeline.row_reader import iter_extra_feature_rows

        prop = PropertyRecord.objects.create(
            address="10 EXTRA ST",
            city="Houston",
            zipcode="77001",
            account_number="E1",
            state_class="A1",
            is_residential=True,
        )
        account_map = {"E1": prop.id}

        now_iso = timezone.now().isoformat()
        batch_id = timezone.now().strftime("%Y%m%d_%H%M%S")

        # COPY path
        with TemporaryDirectory() as d:
            path = Path(d) / "extra_features.txt"
            path.write_text("\n".join(self.FIXTURE_LINES) + "\n", encoding="latin-1")
            copy_result = copy_load(
                table="data_extrafeature",
                columns=self.DB_COLUMNS,
                row_gen=iter_extra_feature_rows(path, account_map=account_map),
                truncate=True,
                extra_columns={
                    "import_date": now_iso,
                    "import_batch_id": batch_id,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                },
            )
        copy_rows = {
            (f.account_number, f.feature_code, f.feature_number): f
            for f in ExtraFeature.objects.all()
        }

        # ORM path
        config = ETLConfig(
            download_dir=Path("/tmp/_parity_efd"),
            extract_dir=Path("/tmp/_parity_efe"),
            log_dir=Path("/tmp/_parity_efl"),
        )
        loader = ModelLoader(config, batch_size=10)
        with TemporaryDirectory() as d:
            path = Path(d) / "extra_features.txt"
            path.write_text("\n".join(self.FIXTURE_LINES) + "\n", encoding="latin-1")
            orm_result = loader.bulk_load(
                model_class=ExtraFeature,
                row_gen=iter_extra_feature_rows(path, account_map=account_map),
                field_names=self.ORM_FIELD_NAMES,
                truncate=True,
                extra_fields={
                    "import_date": timezone.now(),
                    "import_batch_id": batch_id,
                },
            )
        orm_rows = {
            (f.account_number, f.feature_code, f.feature_number): f
            for f in ExtraFeature.objects.all()
        }

        # Assert identical results
        self.assertEqual(copy_result["invalid"], orm_result.records_invalid)
        self.assertEqual(copy_result["skipped"], orm_result.records_skipped)
        self.assertEqual(len(copy_rows), 2)
        self.assertEqual(len(copy_rows), len(orm_rows))
        self.assertEqual(set(copy_rows.keys()), set(orm_rows.keys()))

        for key in copy_rows:
            c = copy_rows[key]
            o = orm_rows[key]
            self.assertEqual(c.property_id, o.property_id, f"property_id mismatch for {key}")
            self.assertEqual(c.feature_code, o.feature_code, f"feature_code mismatch for {key}")
            self.assertEqual(
                c.feature_number, o.feature_number, f"feature_number mismatch for {key}"
            )
            self.assertEqual(c.quality_code, o.quality_code, f"quality_code mismatch for {key}")
            self.assertEqual(
                c.condition_code, o.condition_code, f"condition_code mismatch for {key}"
            )
            self.assertEqual(c.year_built, o.year_built, f"year_built mismatch for {key}")
            self.assertEqual(c.is_active, o.is_active, f"is_active mismatch for {key}")
            for field in ["quantity", "area", "length", "width", "value"]:
                cv = getattr(c, field)
                ov = getattr(o, field)
                if cv is None or ov is None:
                    self.assertEqual(cv, ov, f"{field} mismatch for {key}: {cv} vs {ov}")
                else:
                    self.assertAlmostEqual(
                        float(cv),
                        float(ov),
                        places=2,
                        msg=f"{field} mismatch for {key}: {cv} vs {ov}",
                    )
