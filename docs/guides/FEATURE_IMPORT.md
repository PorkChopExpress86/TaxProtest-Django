# HCAD Extra Feature Import

This guide covers importing HCAD extra features such as `Gunite Pool`, `Frame Detached Garage`, sheds, carports, and other appraised improvements into `data_extrafeature`.

## Source Files

Feature data comes from the `Real_building_land` extract:

- `var/extracted/Real_building_land/extra_features_detail1.txt`
- `var/extracted/Real_building_land/extra_features_detail2.txt`
- `var/extracted/Real_building_land/extra_features.txt` as a fallback format

The modern ETL prefers the detail files when they exist. Detail files contain the useful display fields:

- `dscr` -> `ExtraFeature.feature_description`
- `units` -> `quantity`
- `length`, `width`
- `cond_cd` -> `condition_code`
- `act_yr` -> `year_built`
- `asd_val` -> `value`

The fallback file uses `l_dscr`, `count`, and `uts` for description, quantity, and value.

## Import Workflow

Run from the repository root:

```bash
docker compose run --rm --user root web python manage.py import_all_data --skip-download --skip-property --skip-gis
```

This rebuilds building details, room counts, and extra features from the existing extracted files. It also refreshes `PropertyRecord.is_data_ready` and runs strict building validation.

If the feature rows import but strict building completeness fails because the current property dataset is incomplete, the feature data may still be loaded. Check the feature-specific validation below before rerunning or changing code. For debugging an incomplete dataset where strict validation is expected to fail, the alternate modular command can be run in partial mode:

```bash
docker compose run --rm --user root web python manage.py etl_pipeline run --scope building-only --skip-download --skip-extract --allow-partial
```

Use the strict `import_all_data` path for normal production refreshes.

## Validate Features

After the import, verify that feature descriptions and details are populated:

```bash
docker compose run --rm --user root web python manage.py shell -c "from data.models import ExtraFeature; from django.db.models import Count; print('literal_none', ExtraFeature.objects.filter(is_active=True, feature_description__iexact='none').count()); print('active_features', ExtraFeature.objects.filter(is_active=True).count()); print('pool_examples', list(ExtraFeature.objects.filter(is_active=True, feature_description__icontains='Pool').values('feature_code','feature_description','quantity','length','width','value')[:5])); print('garage_examples', list(ExtraFeature.objects.filter(is_active=True, feature_description__icontains='Garage').values('feature_code','feature_description','quantity','length','width','value')[:5])); print('top_desc', list(ExtraFeature.objects.filter(is_active=True).values('feature_description').annotate(c=Count('id')).order_by('-c')[:10]));"
```

Expected signs of a good import:

- `literal_none 0`
- pool examples include descriptions such as `Gunite Pool`
- garage examples include descriptions such as `Frame Detached Garage`
- examples include non-null `quantity`, `length`, `width`, and `value` where HCAD provides them

For full dataset readiness, run:

```bash
docker compose run --rm --user root web python manage.py validate_data --skip-gis-checks
```

Feature import can be correct even if readiness validation reports unrelated missing building or room data.

## Tests

Run the feature-focused regression tests after changing the import mapping:

```bash
docker compose run --rm --user root web python manage.py test data.etl_pipeline.tests.test_integration.TestDataTransformerIntegration data.etl_pipeline.tests.test_integration.TestModelLoaderExtraFeatures data.tests.test_residential_etl.ETLLoaderOptimizationTests -v 2
```

Before handing off a broader import change, run:

```bash
docker compose run --rm --user root web python manage.py test -v 2
```

## Troubleshooting

If every feature displays as `None` or `None (3)`, inspect the active rows:

```bash
docker compose run --rm --user root web python manage.py shell -c "from data.models import ExtraFeature; from django.db.models import Count; print(list(ExtraFeature.objects.filter(is_active=True).values('feature_description').annotate(c=Count('id')).order_by('-c')[:10]));"
```

If the top description is literal `None`, the import likely read the wrong source columns or stringified Python `None`. Verify the mappings in `data/etl_pipeline/transform.py`, `data/etl_pipeline/model_loader.py`, and `data/etl.py`, then rerun the focused tests and import workflow.
