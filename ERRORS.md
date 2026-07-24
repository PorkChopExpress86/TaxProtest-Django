# ERRORS.md

Known errors, symptoms, causes, and fixes. Check here before diving into logs.

---

## Docker / Compose

### Celery beat exits immediately on startup

**Symptom:** `beat` container exits with a lock/database error referencing `celerybeat-schedule`.
**Cause:** `celerybeat-schedule-shm` and `celerybeat-schedule-wal` were left behind by an unclean container stop.
**Fix:**
```bash
rm celerybeat-schedule*   # in project root
docker compose up beat
```

### Migrations not reflected in container

**Symptom:** DB errors about missing columns or tables after adding a migration file.
**Cause:** `makemigrations` runs on the host but `migrate` must run inside the container. The entrypoint applies migrations on startup, but only if the image was rebuilt after the new migration was created.
**Fix:** `docker compose run --rm taxprotest-dev python manage.py migrate`, or rebuild the image.

### `staticfiles/` missing / 500 on static assets

**Symptom:** Static assets 404 or Django raises `ValueError: Missing staticfiles manifest entry`.
**Cause:** `staticfiles/` is generated at build time by `collectstatic`; it is not committed to git and is absent if the image wasn't rebuilt.
**Fix:** `make build` (or `docker compose up --build`). Never commit `staticfiles/`.

---

## Data / ETL

### Tax impact shows `completeness="missing"` / $0 amounts

**Symptom:** `/protest/<account>/` renders with zero dollar amounts and `completeness="missing"`.
**Cause:** `TaxUnitRate` and/or `PropertyJurisdictionExemption` have no rows for the relevant tax year. These are **not** populated by `import_all_data`.
**Fix:** Run the two import commands with TSV files for the target year:
```bash
docker compose run --rm taxprotest-dev python manage.py import_tax_unit_rates --path /path/to/rates.tsv --tax-year 2025
docker compose run --rm taxprotest-dev python manage.py import_jur_exemptions --path /path/to/exemptions.tsv --tax-year 2025
```

### `import_all_data` fails completeness check

**Symptom:** Command exits non-zero with a completeness-contract error after loading.
**Cause:** Partial download (network interruption, corrupt archive) produced incomplete data. Also possible if a new non-null column was added to `PropertyRecord`/`BuildingDetail` without updating `fast_loader.py` (see below).
**Fix:** Re-run `download_hcad` or manually place the source files under `var/downloads/`, then re-import. For `fast_loader.py` null violations, see [PostgreSQL COPY null constraint](#postgresql-copy-null-constraint).

### GIS coordinates missing → similarity / protest 404

**Symptom:** Similarity and protest analysis pages raise a "location data required" error for a property.
**Cause:** `load_gis_data` has not been run, or ran before `load_hcad_real_acct` created the property rows.
**Fix:** Ensure `load_hcad_real_acct` runs first, then `load_gis_data`. `import_all_data` handles the correct order automatically.

### Properties invisible to search / similarity views

**Symptom:** Known property account returns no results.
**Cause:** `is_residential=False` or `is_data_ready=False` on the row — both flags must be true for the queryable contract.
**Fix:**
```bash
docker compose run --rm taxprotest-dev python manage.py validate_data         # diagnose
docker compose run --rm taxprotest-dev python manage.py reconcile_property_data --apply  # fix
```

### ETL re-download appears to be skipped

**Symptom:** `DownloadManager` reports "skipping" but the local archive is known corrupt or outdated.
**Cause:** `DownloadManager` HEAD-compares `Content-Length` + `Last-Modified`; if they match the local file, it skips.
**Fix:** Set `ETL_FORCE_DOWNLOAD=1` in the environment before running import.

### `import_all_data --skip-download` still re-extracts

**Symptom:** Passing `--skip-download` to `import_all_data` doesn't skip extraction.
**Cause:** `--skip-download` and `--skip-extract` are independent flags. The startup entrypoint skips download but not extract intentionally (the `./var:/app/var` bind-mount shadows build-time downloads).
**Fix:** Pass `--skip-extract` explicitly if you also want to skip extraction.

---

## PostgreSQL COPY null constraint

**Symptom:** `import_all_data` fails with a PostgreSQL null-constraint violation on `PropertyRecord` or `BuildingDetail` during the fast-load stage.
**Cause:** A new non-null column was added to the model but not added to the explicit column list in `data/etl_pipeline/fast_loader.py`. The `COPY FROM` path bypasses Django's model `save()` and does not fill in defaults automatically.
**Fix:** Add the new column to the COPY column list and `COPY` value generation in `fast_loader.py`.

---

## Tests

### `manage.py test` silently skips etl_pipeline tests

**Symptom:** Tests in `data/etl_pipeline/tests/` appear to pass but nothing runs.
**Cause:** Plain pytest-style classes (not subclassing `unittest.TestCase`) are silently skipped by `manage.py test`.
**Fix:** Use `make test` (→ `pytest`) as the canonical runner. When adding tests to `data/tests/` that must also run under `manage.py test`, subclass `django.test.TestCase`.

### Test fails with "returned `<object>`, not a test"

**Symptom:** `manage.py test path.to.TestClass` raises this error.
**Cause:** The class is a plain pytest-style class, not a `unittest.TestCase` subclass. `manage.py test` cannot collect it.
**Fix:** Run via `pytest` in the `taxprotest-dev` container, not `manage.py test`.

---

## Resolved (historical reference)

### `export_csv` NameError on `similar` variable — RESOLVED

**Symptom:** `export_csv` view raised `NameError: name 'similar' is not defined`.
**Cause:** A `protest_analysis_export` block was accidentally pasted into `export_csv`, referencing names out of scope.
**Fix:** Deleted the stray block. `export_csv` only writes a property-search CSV.
**Commit:** `6c8eaa1` (2026-06-05)
