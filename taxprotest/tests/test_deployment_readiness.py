"""Unit tests for check_deployment_readiness management command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase


class DeploymentReadinessCommandTests(TestCase):
    def test_readiness_passes_on_clean_environment(self):
        out = StringIO()
        call_command("check_deployment_readiness", stdout=out)
        output = out.getvalue()
        self.assertIn("Database connectivity verified", output)
        self.assertIn("Migrations are fully up to date", output)
        self.assertIn("Core tables present", output)
        self.assertIn("Celery beat schedule verified", output)
        self.assertIn("All deployment readiness checks passed", output)

    def test_readiness_fails_when_celery_task_unreachable(self):
        from taxprotest.celery import app as celery_app

        original = dict(celery_app.conf.beat_schedule)
        try:
            celery_app.conf.beat_schedule = {
                "broken-task": {
                    "task": "nonexistent.module.nonexistent_task",
                }
            }
            with self.assertRaises(CommandError) as ctx:
                call_command("check_deployment_readiness")
            self.assertIn(
                "unreachable task 'nonexistent.module.nonexistent_task'", str(ctx.exception)
            )
        finally:
            celery_app.conf.beat_schedule = original

    def test_readiness_fails_when_core_table_missing(self):
        with patch(
            "django.db.backends.base.introspection.BaseDatabaseIntrospection.table_names"
        ) as mock_tables:
            mock_tables.return_value = ["auth_user"]  # Missing property & candidate tables
            with self.assertRaises(CommandError) as ctx:
                call_command("check_deployment_readiness")
            self.assertIn("Required core tables are missing", str(ctx.exception))

    def test_readiness_fails_when_unapplied_migrations_exist(self):
        mock_migration = MagicMock()
        mock_migration.app_label = "data"
        mock_migration.name = "0999_fake_migration"

        with patch("django.db.migrations.executor.MigrationExecutor.migration_plan") as mock_plan:
            mock_plan.return_value = [(mock_migration, False)]
            with self.assertRaises(CommandError) as ctx:
                call_command("check_deployment_readiness")
            self.assertIn("Unapplied migrations detected", str(ctx.exception))
            self.assertIn("data.0999_fake_migration", str(ctx.exception))
