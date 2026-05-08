from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("data", "0013_trim_assessmenthistory_columns"),
    ]

    operations = [
        migrations.AddField(
            model_name="assessmenthistory",
            name="appraised_value",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="assessmenthistory",
            name="market_value",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="assessmenthistory",
            name="prior_appraised_value",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="assessmenthistory",
            name="prior_market_value",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="assessmenthistory",
            name="new_construction_value",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="assessmenthistory",
            name="cap_account",
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
