"""Deployment readiness verification command.

Checks:
1. Database connectivity and core tables existence
2. Migration status (no unapplied migrations)
3. Celery beat schedule task resolution
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.utils.module_loading import import_string

CORE_TABLES = (
    "auth_user",
    "data_propertyrecord",
    "brazos_cad_propertyaccount",
    "data_importoperation",
    "data_importcandidate",
)


class Command(BaseCommand):
    help = "Verify database connectivity, migrations, and Celery beat schedules prior to production traffic."

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="Database connection alias to verify (default: default)",
        )

    def handle(self, *args, **options):
        db_alias = options["database"]
        self.stdout.write("Running deployment readiness checks...")

        # 1. Database connectivity
        self._check_database_connectivity(db_alias)

        # 2. Core tables
        self._check_core_tables(db_alias)

        # 3. Migration status
        self._check_migrations(db_alias)

        # 4. Celery beat schedule
        self._check_celery_schedule()

        self.stdout.write(self.style.SUCCESS("All deployment readiness checks passed."))

    def _check_database_connectivity(self, db_alias: str) -> None:
        try:
            connection = connections[db_alias]
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            raise CommandError(
                f"Database connectivity check failed on '{db_alias}': {exc}"
            ) from exc
        self.stdout.write(
            self.style.SUCCESS(f"  [OK] Database connectivity verified on '{db_alias}'")
        )

    def _check_migrations(self, db_alias: str) -> None:
        connection = connections[db_alias]
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
        if plan:
            unapplied = [f"{migration.app_label}.{migration.name}" for migration, _ in plan]
            raise CommandError(
                f"Unapplied migrations detected ({len(plan)}): {', '.join(unapplied)}"
            )
        self.stdout.write(self.style.SUCCESS("  [OK] Migrations are fully up to date"))

    def _check_core_tables(self, db_alias: str) -> None:
        connection = connections[db_alias]
        existing_tables = set(connection.introspection.table_names())
        missing_tables = [table for table in CORE_TABLES if table not in existing_tables]
        if missing_tables:
            raise CommandError(
                f"Required core tables are missing from '{db_alias}': {', '.join(missing_tables)}"
            )
        self.stdout.write(
            self.style.SUCCESS(f"  [OK] Core tables present ({len(CORE_TABLES)} verified)")
        )

    def _check_celery_schedule(self) -> None:
        try:
            from taxprotest.celery import app as celery_app
        except Exception as exc:
            raise CommandError(f"Could not import Celery app: {exc}") from exc

        schedule = getattr(celery_app.conf, "beat_schedule", {})
        if not schedule:
            self.stdout.write(self.style.WARNING("  [WARN] No Celery beat schedule configured"))
            return

        for name, entry in schedule.items():
            task_name = entry.get("task")
            if not task_name:
                raise CommandError(f"Celery beat schedule entry '{name}' missing 'task' attribute")
            try:
                import_string(task_name)
            except Exception as exc:
                raise CommandError(
                    f"Celery beat schedule entry '{name}' specifies unreachable task '{task_name}': {exc}"
                ) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"  [OK] Celery beat schedule verified ({len(schedule)} tasks valid)"
            )
        )
