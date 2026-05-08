from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("data", "0011_propertyrecord_trigram_search_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssessmentHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_number", models.CharField(db_index=True, max_length=20)),
                ("tax_year", models.IntegerField(db_index=True)),
                ("assessed_value", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-tax_year"],
            },
        ),
        migrations.AddConstraint(
            model_name="assessmenthistory",
            constraint=models.UniqueConstraint(fields=("account_number", "tax_year"), name="unique_assessment_history_per_year"),
        ),
    ]
