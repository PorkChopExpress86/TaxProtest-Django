"""Introducing audit storage preserves existing county facts and identities."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ImportMigrationTests(TransactionTestCase):
    def test_brazos_gross_base_repair_preserves_other_rows(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [("data", "0021_import_coverage_permission")]
        try:
            executor.migrate(before)
            model = executor.loader.project_state(before).apps.get_model(
                "data", "PropertyJurisdictionExemption"
            )
            rows = []
            for county, source, code, assessed in (
                ("brazos", "APPRAISAL_ENTITY_INFO.TXT", "", 242613),
                ("brazos", "APPRAISAL_ENTITY_INFO.TXT", "OV65", 242613),
                ("harris", "APPRAISAL_ENTITY_INFO.TXT", "", 242613),
                ("brazos", "Legacy unknown", "", 242613),
                ("brazos", "APPRAISAL_ENTITY_INFO.TXT", "", None),
            ):
                rows.append(
                    model.objects.create(
                        account_number="REPAIR0" if len(rows) < 2 else f"REPAIR{len(rows)}",
                        county=county,
                        source=source,
                        exemption_code=code,
                        assessed_value=assessed,
                        taxable_value=167613,
                        tax_year=2026,
                        tax_unit_code="G1",
                        exemption_amount=75000 if code else None,
                    )
                )
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            model = executor.loader.project_state(latest).apps.get_model(
                "data", "PropertyJurisdictionExemption"
            )
            self.assertEqual(
                [model.objects.get(pk=row.pk).taxable_value for row in rows],
                [242613, 167613, 167613, 167613, None],
            )
            self.assertEqual(model.objects.get(pk=rows[1].pk).exemption_amount, 75000)
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_audit_migration_preserves_existing_property(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [
            ("data", "0016_remove_assessmenthistory_unique_assessment_history_per_year_and_more")
        ]
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            prop = apps.get_model("data", "PropertyRecord").objects.create(
                account_number="LEGACY", address="Recorded address", is_residential=True
            )
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            apps = executor.loader.project_state(latest).apps
            restored = apps.get_model("data", "PropertyRecord").objects.get(pk=prop.pk)
            self.assertEqual(restored.address, "Recorded address")
            self.assertEqual(restored.account_number, "LEGACY")
            self.assertFalse(apps.get_model("data", "ImportOperation").objects.exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
