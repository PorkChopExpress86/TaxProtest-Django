"""Repair known PACS net bases without guessing unverified exemptions."""

from decimal import Decimal

from django.db import migrations


def repair_bases(apps, schema_editor):
    model = apps.get_model("data", "PropertyJurisdictionExemption")
    alias = schema_editor.connection.alias
    rows = model.objects.using(alias)
    for base in rows.filter(
        county="brazos", exemption_code="", source__endswith="APPRAISAL_ENTITY_INFO.TXT"
    ).iterator(chunk_size=1000):
        companions = rows.filter(
            county="brazos",
            account_number=base.account_number,
            tax_year=base.tax_year,
            tax_unit_code=base.tax_unit_code,
        ).exclude(exemption_code="")
        verified = sum(row.exemption_amount or Decimal(0) for row in companions)
        if (
            base.assessed_value is None
            or base.taxable_value is None
            or max(Decimal(0), base.assessed_value - verified) != base.taxable_value
        ):
            rows.get_or_create(
                county="brazos",
                account_number=base.account_number,
                tax_year=base.tax_year,
                tax_unit_code=base.tax_unit_code,
                exemption_code="UNVERIFIED",
                defaults={
                    "source": base.source,
                    "exemption_description": "Source net value does not reconcile with verified exemptions",
                },
            )
        base.taxable_value = base.assessed_value
        base.save(using=alias, update_fields=["taxable_value"])


class Migration(migrations.Migration):
    dependencies = [("data", "0021_import_coverage_permission")]
    operations = [migrations.RunPython(repair_bases, migrations.RunPython.noop)]
