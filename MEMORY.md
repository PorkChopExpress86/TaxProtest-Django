# MEMORY.md

Key architectural decisions, invariants, and non-obvious context for this project. Complements CLAUDE.md (which covers structure/commands) and ERRORS.md (which covers known failure modes).

---

## Architectural Decisions

### ETL two-layer design

`data/etl_pipeline/` is the orchestrated, modern pipeline — `ETLOrchestrator.execute()` owns the full download → extract → transform → load → contract-validation flow and is used by `import_all_data` and the Celery beat tasks.

`data/etl.py` is the lower-level helper layer — `bulk_load_properties`, `load_building_details`, `load_extra_features`, `load_gis_parcels`, etc. These are called by the granular `load_*` / `import_*` management commands and are **not** legacy; they complement the pipeline package.

If adding a new bulk-load step, decide which layer owns it: pipeline-stage work goes in `etl_pipeline/`; standalone "import this one thing" commands stay in `etl.py` + a management command.

### PostgreSQL COPY fast path

`data/etl_pipeline/fast_loader.py` (`copy_load_property_records`, `copy_load_building_details`) uses `COPY FROM` via a `StringIO` buffer, bypassing Django ORM. This is Postgres-only — the orchestrator falls back to the generic loader for SQLite (tests).

**Critical invariant:** COPY bypasses Django's model `save()`. Any non-null column that Django normally fills automatically (blank-default `CharField`, auto `DateTimeField`, custom default) **must** be listed explicitly in the COPY column list in `fast_loader.py`. Adding a new non-null field to `PropertyRecord` or `BuildingDetail` requires updating `fast_loader.py` or the import will fail with a null-constraint violation.

GIS data uses a temp-table + `UPDATE ... FROM` pattern (`data/etl.py: load_gis_parcels`), also Postgres-only.

### Assessment history is the history of record; buildings/features are not

`AssessmentHistory` rows accumulate per-year data and are the canonical record for historical assessed/appraised/market values. `BuildingDetail` and `ExtraFeature` are snapshot-only — they are fully replaced on each import cycle, not versioned. Do not add year-keyed building history rows.

### `is_residential + is_data_ready` contract

Every query path that surfaces property data to users relies on both flags being true. `is_residential` is set by `residential.py: is_residential_state_class()` during property import. `is_data_ready` is set by `etl.py: refresh_property_readiness()` after all sub-imports complete. A property missing either flag is invisible to search/similarity/protest views — this is intentional and documented in `validate_data`. Use `reconcile_property_data --apply` to clean legacy rows.

### Tax impact requires two pre-populated tables

`calculate_tax_impact()` in `data/tax_impact.py` returns `completeness="missing"` — and shows $0 — if `TaxUnitRate` or `PropertyJurisdictionExemption` rows don't exist for the queried tax year. These are **not** populated by `import_all_data`; they require separate management commands (`import_tax_unit_rates`, `import_jur_exemptions`) run with TSV files from HCAD. This is a known setup gap for fresh environments.

### ETL downloads skip unchanged archives

`DownloadManager` HEAD-compares `Content-Length` + `Last-Modified` against the local file before re-downloading. If a forced re-download is needed (e.g. data was corrupted locally), set `ETL_FORCE_DOWNLOAD=1`.

### Build-time vs. runtime data

`scripts/build_time_download.py` downloads HCAD source files at image build time into `/app/var/downloads/`. However, the `docker-compose.yml` bind-mounts `./var:/app/var`, which **shadows** the baked-in downloads at runtime. The `entrypoint.sh` therefore passes `--skip-download` but **not** `--skip-extract` — extraction still runs at startup, against whatever is present in the host-side `var/downloads/`.

---

## Test Runner Invariant

All tests in `data/tests/` and `taxprotest/tests/` subclass `django.test.TestCase`. They can be run with either `manage.py test` or `pytest`.

Tests in `data/etl_pipeline/tests/` may include plain pytest-style classes — these are **silently skipped** by `manage.py test`. The canonical test runner is `make test` (→ `pytest` in the `taxprotest-dev` dev image), which catches both.

---

## Compose Service Profiles

| Profile | Services | When used |
|---|---|---|
| *(default)* | `postgres`, `redis`, `migrate`, `web`, `worker`, `beat` | Production / standard dev |
| `devtools` | `taxprotest-dev` | Lint, format, type-check, test |
| `jobs` | `ingest`, `refresh` | One-shot ETL runs |

`make ingest` / `make refresh` start the `jobs` profile services. Lint/test tooling lives **only** in the `devtools` profile image (built from the `dev` Dockerfile target). Do not attempt to run `pytest` or `mypy` in the `web` container.

---

## validate_data Tolerances

`validate_data` uses tolerance-based completeness checks, not zero-tolerance. GIS coverage, missing buildings/rooms, and not-ready properties pass within a small percentage. Thresholds are env-overridable:

- `VALIDATE_MIN_GIS_COVERAGE_PCT` — default 99.0
- `VALIDATE_MAX_MISSING_BUILDING_PCT` — default 1.0
- `VALIDATE_MAX_MISSING_ROOM_PCT` — default 1.0
- `VALIDATE_MAX_NOT_READY_PCT` — default 1.0

Structural checks (empty tables, duplicates, non-residential rows) always hard-fail regardless of tolerance settings.
