from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("data", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="propertyrecord",
            index=models.Index(fields=["zipcode"], name="property_zip_idx"),
        ),
        migrations.AddIndex(
            model_name="propertyrecord",
            index=models.Index(fields=["address"], name="property_addr_idx"),
        ),
    ]
