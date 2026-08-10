from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("data", "0012_assessmenthistory"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE data_assessmenthistory
                DROP COLUMN IF EXISTS initial_appraised_value CASCADE,
                DROP COLUMN IF EXISTS final_appraised_value CASCADE,
                DROP COLUMN IF EXISTS market_value CASCADE,
                DROP COLUMN IF EXISTS protested CASCADE,
                DROP COLUMN IF EXISTS hearing_scheduled_date CASCADE,
                DROP COLUMN IF EXISTS hearing_actual_date CASCADE,
                DROP COLUMN IF EXISTS hearing_release_date CASCADE,
                DROP COLUMN IF EXISTS hearing_type CASCADE,
                DROP COLUMN IF EXISTS value_source CASCADE,
                DROP COLUMN IF EXISTS property_id CASCADE
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
