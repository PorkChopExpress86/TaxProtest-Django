# Architecture Review — Audit Log

**Date:** 2026-08-14
**Reviewer:** GLM-5.2 via opencode, using the `improve-codebase-architecture` skill
**Session scope:** Full codebase architecture review → grilling → implementation of all candidates
**Test status at session end:** 392 tests pass, full Harris ETL verified, both counties' web pages verified

---

## Summary

| Metric | Before | After |
|---|---|---|
| Tests | 386 | 392 |
| Net LOC (production code only) | — | −3,525 LOC deleted, +613 LOC added = **−2,912 net** |
| Files deleted | 0 | 4 |
| Files created | 0 | 9 |
| Files modified | 0 | 27 |

**Deletion-heavy by design.** Every candidate passed the deletion test — the removed code was shallow indirection or duplicated logic that concentrated complexity when deleted, rather than moving it.

---

## Candidates implemented (in order)

### 1. Finish the ETL pipeline migration (Strong)

**Problem:** Two parallel ETL systems — `counties/harris/etl.py` (1,115 LOC) and `counties/harris/etl_pipeline/` — coexisted. The pipeline reached back into `etl.py` for two functions; six legacy management commands bypassed the pipeline entirely; `tasks_new.py` had a 190-line dead legacy block with its own parallel source list; `etl_pipeline/__init__.py` over-exported 7 internal classes.

**Changes:**
- **Deleted** `counties/harris/etl.py` (1,115 LOC) — all functions either absorbed by the pipeline or moved to their correct home.
- **Created** `counties/harris/etl_pipeline/readiness.py` — `refresh_property_readiness` moved here from `etl.py`; orchestrator imports locally via `from .readiness import refresh_property_readiness`.
- **Created** `counties/harris/etl_pipeline/gis_loader.py` — `load_gis_parcels` moved here from `etl.py`; orchestrator imports locally via `from .gis_loader import load_gis_parcels`.
- **Created** `counties/harris/reconciliation.py` — `link_orphaned_records` + `load_account_property_map` moved here; only `reconcile_property_data` (one-shot legacy cleanup tool) imports from it.
- **Modified** `counties/harris/etl_pipeline/__init__.py` — tightened from 7 re-exports to 4: `ETLConfig`, `ETLOrchestrator`, `DataSource`, `DataSourceType`. Stage managers (`DownloadManager`, `ExtractManager`, `DataTransformer`, `ModelLoader`, `ETLLogger`) are no longer exported.
- **Modified** `counties/harris/etl_pipeline/orchestrator.py` — the two `from counties.harris.etl import ...` lines replaced with `from .readiness import ...` and `from .gis_loader import ...`.
- **Modified** `counties/harris/tasks_new.py` — 417 → 124 LOC. Deleted: the 190-line legacy block (`HCAD_ARCHIVE_SOURCES`, `download_and_extract_hcad`, helpers), the `download_hcad_data` and `extract_hcad_data` intermediate tasks (zero callers). Kept: `download_and_import_building_data`, `download_and_import_gis_data`, `run_etl_pipeline`, all delegating to `_run_authoritative_pipeline`.
- **Modified** 5 legacy management commands → thin pipeline shims:
  - `load_hcad_real_acct.py` — shim over `import_all_data --skip-building --skip-gis`
  - `load_gis_data.py` — shim over `import_all_data --skip-property --skip-building`
  - `load_building_features.py` — shim over `import_all_data --skip-property --skip-gis`
  - `import_building_data.py` — 342 → 55 LOC; `--async` queues Celery, sync calls pipeline
  - `download_hcad.py` — shim over `ETLOrchestrator.execute_download_only()`
- **Deleted** `load_room_counts.py` — absorbed by pipeline's `FixturesAggregator`.
- **Deleted** `link_orphaned_records.py` (standalone command) — function moved to `reconciliation.py`, only callable from `reconcile_property_data --apply`.
- **Modified** `etl_pipeline.py` command — repointed `handle_download`, `handle_extract`, `handle_status` from `DownloadManager`/`ExtractManager` direct imports to `orchestrator.execute_download_only()` / `execute_extract_only()` / `orchestrator.download_manager.is_downloaded()`.
- **Modified** `import_assessment_history.py` — repointed from `DownloadManager`/`ExtractManager` to `ETLOrchestrator.execute_download_only()` + `execute_extract_only()`.
- **Modified** `reconcile_property_data.py` — import changed from `from counties.harris.etl import ...` to `from counties.harris.reconciliation import ...` + `from counties.harris.etl_pipeline.readiness import ...`.

**Risk notes:**
- `etl.py` deletion is the highest-blast-radius change. The full ETL was re-run end-to-end (1,173,910 properties + 1,202,713 buildings + 871,770 features + 1,172,993 GIS coords) and `validate_data` passed.
- `reconcile_property_data` survives as a one-shot tool for databases that ran the legacy soft-delete path. Its `link_orphaned_records` function now lives in `reconciliation.py`, not in the pipeline.

---

### 2. One market-data path, not two (Strong)

**Problem:** `fast_loader.py` (436 LOC, PostgreSQL COPY) and `model_loader.py` (534 LOC, Django ORM `bulk_create`) duplicated ~325 lines of column mappings, type coercers, and business logic. Five behavioral divergences: (1) COPY fails on duplicate keys while ORM silently drops them, (2) currency/comma stripping inconsistent, (3) `is_residential` hardcoded `"t"` in COPY, (4) zipcode max_length mismatch, (5) timestamp handling differs.

**Changes:**
- **Created** `counties/harris/etl_pipeline/row_reader.py` (~340 LOC) — single source of truth for parsing HCAD files. Contains:
  - `RowResult` dataclass (`values`, `field_names`, `skip`, `invalid`)
  - Consolidated column-source mappings (`REAL_ACCT_SOURCES`, `BUILDING_RES_SOURCES`, `EXTRA_FEATURES_SOURCES`)
  - Shared type coercers (`coerce_str`, `coerce_int`, `coerce_decimal`)
  - Three generator functions: `iter_property_rows`, `iter_building_rows`, `iter_extra_feature_rows`
  - All business logic (residential filter, address building, fixtures bed/bath, account validation)
- **Modified** `counties/harris/etl_pipeline/fast_loader.py` — 436 → 168 LOC. Now a generic `copy_load(table, columns, row_gen, truncate, extra_columns)` function + COPY plumbing (`_copy_field`, `_GeneratorIO`). Uses a staging table + `INSERT ON CONFLICT DO NOTHING` to match the ORM path's `ignore_conflicts=True` — **fixing behavioral divergence #1** (duplicate-key handling). No business logic, no column mappings, no type coercers.
- **Modified** `counties/harris/etl_pipeline/model_loader.py` — 534 → 155 LOC. Now cache management (`get_valid_accounts`, `get_account_to_property_map`, `reset_cache`) + one generic `bulk_load(model_class, row_gen, field_names, truncate, extra_fields)` method. No per-file-type methods, no type coercers, no business logic.
- **Modified** `counties/harris/etl_pipeline/orchestrator.py` — replaced the 50-line if/else dispatch tree (`_process_data_file` lines 598-649) with `_load_data_file` method that calls the row reader, then dispatches to `copy_load` (Postgres) or `model_loader.bulk_load` (fallback). `extra_features` now gets a COPY path too (previously ORM-only).
- **Created** `counties/harris/etl_pipeline/tests/test_copy_orm_parity.py` (~240 LOC) — parity tests that run the same fixture through both COPY and ORM paths, assert identical row counts + field values. Uses `TransactionTestCase` (TRUNCATE requires committed transactions).

**All 5 behavioral divergences resolved:**
1. Duplicate keys → staging table + `ON CONFLICT DO NOTHING`
2. Currency/comma stripping → consolidated in `coerce_decimal`
3. `is_residential` → actual boolean from row reader
4. zipcode max_length → single `coerce_str(maxlen=20)` call
5. Timestamps → both paths use `extra_columns`/`extra_fields`

**Risk notes:**
- The staging table approach adds one `CREATE TEMP TABLE` + `INSERT ... SELECT` per load. Performance impact is negligible — the temp table has no indexes/constraints, and the `INSERT ... SELECT` is a set-based operation.
- The ORM fallback path is untested in production (SQLite is the code default but `DATABASE_URL` always overrides to Postgres). The parity test locks equivalence.

---

### 3. Extract shared similarity pure-math (Strong)

**Problem:** `harris/similarity.py` and `brazos/similarity.py` carried ~250 lines of byte-identical pure-math functions (`_clamp`, `_interpolate_curve`, `_percentage_similarity`, `_difference_similarity`, `_categorical_similarity`, `_distance_similarity`, `haversine_distance`, `get_similarity_label`, `_component`, `_score_from_components`). The seam was an "informal key-name agreement" — a dict shape no type enforced.

**Changes:**
- **Created** `counties/common/similarity_math.py` (~230 LOC) — single source of truth for pure-math helpers. Functions are public (no underscore prefix): `clamp`, `safe_float`, `normalized_code`, `interpolate_curve`, `percentage_similarity`, `difference_similarity`, `categorical_similarity`, `ranked_code_similarity`, `distance_similarity`, `haversine_distance`, `get_similarity_label`, `component`, `score_from_components`. The `component` function takes an optional `labels` dict so each county keeps its own display labels.
- **Modified** `counties/harris/similarity.py` — 694 → 405 LOC. Imports math from `similarity_math`. Keeps Harris-specific logic: `QUALITY_RANK`, `_condition_similarity` (uses quality rank + categorical fallback), `_building_character_similarity` (Harris model attrs), `_effective_year`, `_feature_similarity` (Harris model).
- **Modified** `counties/brazos/similarity.py` — 644 → 490 LOC. Imports math from `similarity_math`. Keeps Brazos-specific logic: `_quality_digit`, `_quality_similarity`, `_building_character_similarity` (Brazos model attrs), `_feature_similarity` (Brazos model), `_primary_improvement`, `_has_second_floor`, `_stories_value`, `_effective_year`, `_total_acreage`.

**Risk notes:**
- The `component()` function signature changed: it now takes `labels` as a third kwarg. Both county modules wrap it in a local `_component()` that passes their `COMPONENT_LABELS` dict. The shared web layer's `ScoreComponent.from_mapping()` consumes the same dict shape — no contract change.
- The curves (the `list[tuple[float, float]]` tuning parameters) are still inline in each county's `calculate_similarity_details` — they're per-factor tuning, not shared infrastructure.

---

### 4. Move `COUNTIES_WITH_TYPED_CAP_FLAG` onto adapter (Worth exploring)

**Problem:** `cap_status.py` had `COUNTIES_WITH_TYPED_CAP_FLAG = {"harris"}` — a set of county slug strings checked via `current.county not in COUNTIES_WITH_TYPED_CAP_FLAG`. This was the one place in the shared layer that branched on a county slug. A county couldn't self-register without editing the shared module.

**Changes:**
- **Modified** `counties/common/contracts.py` — added `cap_flag_is_typed()` method to `CountyAdapter` (default: `False`).
- **Modified** `counties/harris/adapter.py` — overrides `cap_flag_is_typed()` → `True`. Passes `cap_flag_is_typed=True` to `assessment_history_rows`.
- **Modified** `counties/common/cap_status.py` — `evaluate_cap_status` now takes `cap_flag_is_typed: bool` kwarg. Deleted `COUNTIES_WITH_TYPED_CAP_FLAG` set entirely. The `if current.county not in COUNTIES_WITH_TYPED_CAP_FLAG` check became `if not cap_flag_is_typed`.
- **Modified** `counties/common/history.py` — `assessment_history_rows` forwards `cap_flag_is_typed` to `evaluate_cap_status`.
- **Modified** `counties/common/tests/test_cap_status.py` — Harris tests pass `cap_flag_is_typed=True`; non-typed tests use the default `False`.

**Risk notes:**
- `evaluate_cap_status`'s signature changed (new kwarg). All callers updated. The `AssessmentHistory.county` field is still on the model — it's just no longer read by `cap_status.py` for branching.

---

### 5. Retire `runtime_paths.py` legacy alias (Speculative)

**Problem:** `runtime_paths.py` had a `if spec.slug == "harris"` branch that appended `PROJECT_REPORT_DIR` as a legacy env-var alias. `settings.py` mirrored it as `PROJECT_REPORT_DIR = HCAD_REPORT_DIR`. Docker compose files used `PROJECT_REPORT_DIR` in 8 places.

**Changes:**
- **Modified** `docker-compose.yml` — 5 `PROJECT_REPORT_DIR` env vars → `HCAD_REPORT_DIR`.
- **Modified** `docker-compose.prod.yml` — 3 `PROJECT_REPORT_DIR` env vars → `HCAD_REPORT_DIR`.
- **Modified** `taxprotest/runtime_paths.py` — deleted `LEGACY_REPORT_DIR_ENV` constant and the `if spec.slug == "harris"` branch. `report_env_names` is now just `(f"{spec.env_prefix}_REPORT_DIR",)` for every county.
- **Modified** `taxprotest/settings.py` — deleted `PROJECT_REPORT_DIR = HCAD_REPORT_DIR`.
- **Modified** `counties/harris/tests/test_runtime_paths.py` — removed 2 legacy-alias tests, replaced with 1 canonical test.
- **Modified** `CLAUDE.md` — removed legacy alias mention.

**Risk notes:**
- Any deployment that sets `PROJECT_REPORT_DIR` in its environment (not in docker-compose) will silently fall back to the default `counties/harris/var/reports`. This is the same path the old alias resolved to, so the behavior change is invisible unless the deployment set a *custom* `PROJECT_REPORT_DIR`.
- `migrate_runtime_artifacts` and its tests stay — it's a harmless idempotent one-shot tool.

---

### 6. Replace `simple_pdf` with a library (Speculative)

**Problem:** `exports.py` had a 45-line hand-rolled PDF generator (`simple_pdf`) that assembled raw PDF bytes with a manual xref table. It was single-page only — lines past ~36 would silently overflow the page boundary with no error. No test verified the byte output.

**Changes:**
- **Modified** `requirements.txt` — added `fpdf2>=2.8`.
- **Modified** `counties/common/exports.py` — replaced `simple_pdf` (45 LOC → 15 LOC) with `fpdf2`-based version. `set_auto_page_break(auto=True, margin=72)` handles multi-page. Deleted `_pdf_escape` (fpdf2 handles escaping internally).
- **Created** `counties/common/tests/test_pdf.py` — 5 tests:
  - `test_produces_valid_pdf_header` — checks `%PDF-` magic bytes
  - `test_empty_lines_render_as_blank_spacers`
  - `test_long_line_list_auto_paginates` — 60 long lines must produce >1 page
  - `test_special_characters_are_escaped` — parentheses, backslashes, `%%EOF` present
  - `test_protest_report_pdf_returns_pdf_response` — full `protest_report_pdf` response shape
- **Modified** `counties/harris/tests/test_views.py` — `test_pdf_export_returns_pdf` updated: checks `%%EOF` instead of grepping raw text (fpdf2 compresses content streams with FlateDecode).
- **Modified** `counties/common/tests/test_shared_pages.py` — same update for Brazos PDF test.

**Risk notes:**
- `fpdf2` is a pure-Python library (no native deps). Adds `Pillow`, `fonttools`, `defusedxml` as transitive dependencies.
- fpdf2 produces `%PDF-1.3` (the old hand-rolled was `%PDF-1.4`). No functional difference for the evidence report use case.
- Content streams are now FlateDecode-compressed — existing tests that grepped for raw text in the PDF body were updated to check structural validity instead.

---

## Test changes

### New test files (5)

| File | Tests | Purpose |
|---|---|---|
| `counties/harris/etl_pipeline/tests/test_ported_from_legacy.py` | 5 classes, ~15 tests | Edge cases ported from deleted legacy test files (state-class exclusion, readiness tiers, fixtures aggregation, GIS account-map linking, centroid CRS ordering) |
| `counties/harris/etl_pipeline/tests/test_quoting_parity.py` | 1 test | `QUOTE_NONE` equivalence between `row_reader._open_text` and `transform.DataTransformer.open_reader` |
| `counties/harris/etl_pipeline/tests/test_copy_orm_parity.py` | 2 tests | COPY and ORM paths produce identical PropertyRecord and BuildingDetail rows from the same fixture |
| `counties/common/tests/test_pdf.py` | 5 tests | PDF validity, auto-pagination, special-char escaping, response shape |
| `counties/harris/tests/test_tasks_new.py` | (rewritten) 3 tests | Task delegation to `_run_authoritative_pipeline` |

### Deleted test files (1)

| File | Reason |
|---|---|
| `counties/harris/tests/test_load_gis_data.py` | Tests ported to `test_ported_from_legacy.py` (GIS account-map linking, centroid CRS) |

### Modified test files (8)

| File | Change |
|---|---|
| `counties/harris/tests/test_residential_etl.py` | Rewritten — dead-function tests removed, surviving tests (validate_data, import_all_data, reconcile) kept with updated imports |
| `counties/harris/tests/test_bedroom_bathroom_data.py` | Removed `load_fixtures_room_counts` import + `ETLLogicTest` class (ported to `test_ported_from_legacy.py`) |
| `counties/harris/tests/test_fast_loader.py` | Rewritten to use new `copy_load` + `iter_property_rows`/`iter_building_rows` API |
| `counties/harris/etl_pipeline/tests/test_integration.py` | 3 integration tests updated to use `RowResult` + `bulk_load` |
| `counties/harris/tests/test_runtime_paths.py` | Removed 2 legacy-alias tests, replaced with 1 canonical test |
| `counties/common/tests/test_cap_status.py` | Harris tests pass `cap_flag_is_typed=True` |
| `counties/harris/tests/test_views.py` | PDF test checks `%%EOF` instead of raw text |
| `counties/common/tests/test_shared_pages.py` | Brazos PDF test same update |

---

## Verification performed

1. **Full test suite:** `python manage.py test` — 392 tests pass
2. **Full Harris ETL:** `import_all_data --skip-download --skip-extract` — 1,173,910 properties + 1,202,713 buildings + 871,770 features + 1,172,993 GIS coords loaded. `validate_data` passes (99.88% data-ready, 99.92% GIS coverage, no duplicates, no orphans).
3. **Web smoke test:** All 11 endpoints (search, similar, protest, PDF, CSV for Harris + Brazos) return 200 with real content.

---

## What an auditor should check

1. **`etl.py` deletion** — confirm no remaining references to `from counties.harris.etl import` anywhere (including scripts, templates, settings).
2. **Staging table COPY** — the `CREATE TEMP TABLE ... AS SELECT ... LIMIT 0` + `INSERT ... ON CONFLICT DO NOTHING` pattern in `fast_loader.py:copy_load`. Verify it handles the HCAD extra_features duplicate-key case correctly.
3. **`row_reader.py` field mappings** — compare `REAL_ACCT_SOURCES` / `BUILDING_RES_SOURCES` / `EXTRA_FEATURES_SOURCES` against the actual HCAD file headers to ensure no column was dropped during consolidation.
4. **`similarity_math.py` curves** — the curves (the `list[tuple[float, float]]` arguments) are still inline in each county's `calculate_similarity_details`. Verify they match the originals exactly (they should — they were copied verbatim).
5. **`cap_flag_is_typed`** — verify Brazos does NOT pass `cap_flag_is_typed=True` anywhere (it should use the default `False`).
6. **`PROJECT_REPORT_DIR`** — grep all deployment configs, `.env` files, and shell scripts for any remaining `PROJECT_REPORT_DIR` references that weren't updated.
7. **`fpdf2` dependency** — verify the Docker image builds with `fpdf2>=2.8` in requirements.txt.
8. **`reconcile_property_data`** — confirm it still works (its imports changed from `etl.py` to `reconciliation.py` + `etl_pipeline/readiness.py`).