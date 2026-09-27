"""Record Brazos source validation under the key every county uses.

Recorded qualification bindings hash the validation value, not its key, so
renaming the key keeps every reviewed Brazos candidate's binding valid.
"""

from django.db import migrations


def _rename(apps, old, new):
    ImportOperation = apps.get_model("data", "ImportOperation")
    for operation in ImportOperation.objects.filter(county="brazos", evidence__has_key=old):
        operation.evidence[new] = operation.evidence.pop(old)
        operation.save(update_fields=["evidence"])


def share_validation_key(apps, schema_editor):
    _rename(apps, "source_validation", "validation")


def restore_brazos_validation_key(apps, schema_editor):
    _rename(apps, "validation", "source_validation")


class Migration(migrations.Migration):

    dependencies = [
        ("data", "0024_import_dataset_recovery_permission"),
    ]

    operations = [
        migrations.RunPython(share_validation_key, restore_brazos_validation_key),
    ]
