from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("data", "0003_remove_propertyrecord_property_zip_idx_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="propertyrecord",
            index=models.Index(fields=["zipcode", "street_number", "street_name"], name="prop_zip_street_idx"),
        ),
        migrations.AddIndex(
            model_name="propertyrecord",
            index=models.Index(fields=["account_number"], name="prop_acct_idx"),
        ),
    ]
