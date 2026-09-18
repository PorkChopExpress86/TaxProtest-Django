"""Introducing audit storage preserves existing county facts and identities."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ImportMigrationTests(TransactionTestCase):
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
