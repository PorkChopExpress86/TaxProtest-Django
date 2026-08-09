# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Django web application for property tax protest analysis across Texas appraisal districts. Searches
properties, finds comparables via similarity scoring, and generates ARB hearing evidence reports with
tax impact estimates. Harris County (HCAD) and Brazos County (BCAD) are supported.

**The organizing rule:** each county's ETL is its own — different source files, different record
layouts, different models. The *web surface* is not. Search, comparables, and the protest report are
implemented once in `counties/common/` and rendered for every county. A county joins by writing an
adapter, never a new view or template.

---

## Architecture

| Layer | Location | Purpose |
|---|---|---|
| Django project | `taxprotest/` | Settings, URLs, Celery config, site-wide views (about, health) |
| Shared web layer | `counties/common/` | Contracts, analysis, charts, views, URL factory, exports |
| Harris County app | `counties/harris/` | HCAD models, ETL, similarity, admin, adapter |
| Brazos County app | `counties/brazos/` | BCAD models, ETL, similarity, parsers, adapter |
| Templates | `templates/` | Tailwind HTML (all templates live here, not inside apps) |
| Scripts | `scripts/` | Entrypoint, build-time download, setup, monitoring helpers |
| Docs | `docs/` | Reference documentation (see below) |

**Django app labels are pinned and differ from the package paths.** `counties.harris` has label
`data`; `counties.brazos` has label `brazos_cad` (see each app's `apps.py`). The packages moved; the
labels did not, so database tables (`data_*`, `brazos_cad_*`), migration history, and content types
are untouched. Migrations and `deletion_totals` keys still say `data.PropertyRecord` — that is
correct, do not "fix" it.

---

## Runtime Data Layout

Every county's downloaded archives, extracted source files, ETL logs, and generated reports live
**inside that county's app directory**:

```
counties/harris/var/{downloads,extracted,logs,reports}/
counties/brazos/var/{downloads,extracted,logs,reports}/
taxprotest/var/                                          # Celery beat schedule
```

Nothing large lands in the project root. `taxprotest/runtime_paths.py` resolves these and every one
is overridable by environment variable — `HCAD_DOWNLOAD_DIR`, `HCAD_EXTRACT_DIR`, `HCAD_LOG_DIR`,
`HCAD_REPORT_DIR`, and the `BCAD_*` equivalents (`PROJECT_REPORT_DIR` is kept as a legacy alias for
Harris). Relative values resolve against the project root.

`scripts/migrate_runtime_artifacts.py` moves any pre-existing runtime data from the old locations
(`var/`, `downloads/`, `data/cad_downloads/`) into the per-county trees. It is idempotent.

---

## Shared Web Layer (`counties/common/`)

| Module | Contents |
|---|---|
| `contracts.py` | `Subject`, `Comp` (county-neutral records); `CountyProfile`, `Column`, `SearchField`, `DetailRow`; the `CountyAdapter` ABC |
| `analysis.py` | `summarize_equity()` → `EquitySummary`; `recommend_protest()` → `ProtestRecommendation`; percentile and YoY helpers |
| `charts.py` | `assessment_history_chart()`, `ppsf_distribution_chart()`, `score_breakdown_summary()` — pure SVG layout data |
| `history.py` | `assessment_history_rows(account_number, county)` over the shared `AssessmentHistory` table |
| `cap_status.py` | `evaluate_cap_status(entry, prior)` — Texas homestead/circuit-breaker cap math; reads `AssessmentHistory.cap_account` differently per county (see `COUNTIES_WITH_TYPED_CAP_FLAG`) |
| `exports.py` | Search CSV, protest-comps CSV, and the hand-rolled `simple_pdf()` evidence report |
| `views.py` | `index`, `export_csv`, `similar_properties`, `protest_analysis`, `protest_analysis_export`, `protest_analysis_pdf` — all take `adapter=` |
| `urls.py` | `county_urlpatterns(adapter)` binds an adapter to the full route set |
| `templatetags/countyfmt.py` | `currency`, `sqft`, `acres`, `field`, `quality_label`, `quality_classes`, `score_classes`, `sort_header` |

`counties.common` is in `INSTALLED_APPS` (label `counties_common`) purely so Django discovers its
template tags. It holds no models and imports no county app at module scope.

### Adding a county

1. Create `counties/<slug>/` as a Django app with its own models, ETL commands, and similarity scoring.
2. Write `counties/<slug>/adapter.py`:
   - a `CountyProfile` — display names, `key_label`, URL prefix/name prefix, search fields, and the
     `Column` specs for the results and comparables tables;
   - a `CountyAdapter` subclass implementing `search_queryset`, `search_rows`, `get_subject`, and
     `find_comps`, optionally `search_context`, `assessment_history`, and `tax_impact`.
3. Add `counties/<slug>/urls.py` calling `county_urlpatterns(adapter)` and include it from
   `taxprotest/urls.py`.
4. Register the runtime directories in `COUNTY_RUNTIME_SPECS` in `taxprotest/runtime_paths.py`.

All six pages, both CSV exports, and the PDF come for free. `counties/common/tests/test_shared_pages.py`
asserts every registered county exposes the identical route set — a new county cannot ship a narrower site.

---

## Key Files

### Harris models (`counties/harris/models.py`)
- `PropertyRecord` — core property record; key flags: `is_residential`, `is_data_ready`
- `BuildingDetail` — building specs (sqft, bedrooms, bathrooms, quality, condition, etc.)
- `ExtraFeature` — pools, garages, patios, etc.
- `AssessmentHistory` — per-year assessed/appraised/market values with cap fields; **county-scoped, shared with Brazos**
- `TaxUnitRate` — annual adopted tax rate per taxing unit code; county-scoped
- `PropertyJurisdictionExemption` — per-account jurisdiction/exemption rows used for tax impact; county-scoped
- `DownloadRecord` — tracks ETL download history

### Harris ETL & analysis (`counties/harris/`)
- `etl.py` — shared ETL helpers (bulk upsert, data-ready marking)
- `residential.py` — `is_residential_state_class()`, `normalize_state_class()`
- `tasks_new.py` — Celery tasks: `download_and_import_building_data`, `download_and_import_gis_data`
- `similarity.py` — similarity scoring algorithm (see Similarity section below)
- `tax_impact.py` — `calculate_tax_impact(account_number, tax_year, median_assessed_value, county)` → `TaxImpactResult`
- `assessment_history.py` — `AssessmentHistoryImporter` for HCAD snapshot import (cap-status evaluation moved to `counties/common/cap_status.py` — it's a shared, county-aware function, not Harris-owned)
- `query.py` — `build_property_search_queryset(params)`
- `adapter.py` — `HARRIS_PROFILE` + `HarrisAdapter`

### Brazos (`counties/brazos/`)
- `models.py` — `PropertyAccount`, `PropertyLand`, `PropertyImprovement`, `PropertyBuildingCharacteristic`, `PropertyExtraFeature`, `PropertyEntity`
- `parsers/pacs.py` — fixed-width offsets for BCAD's certified PACS export
- `similarity.py` — Brazos scoring against `PropertyAccount`
- `adapter.py` — `BRAZOS_PROFILE` + `BrazosAdapter`

### Management Commands
Harris (`counties/harris/management/commands/`):

| Command | Purpose |
|---|---|
| `import_all_data` | Authoritative full ETL — fails hard if completeness not achieved |
| `validate_data` | Enforces residential-only, data-ready contract |
| `reconcile_property_data` | Preview/apply cleanup of legacy mixed/incomplete rows (`--apply`) |
| `load_hcad_real_acct` | Property records only |
| `load_gis_data` | GIS coordinates from HCAD Parcels shapefile |
| `import_building_data` | Building details, features, room counts |
| `load_room_counts` | Room counts only (fixtures.txt) |
| `download_hcad` | Download HCAD source files |
| `import_hcad_jur_exempt` | **Jurisdiction/exemption rows + tax unit rates from `Real_jur_exempt.zip`** (`--tax-year`); this is what makes the Tax Impact section work |
| `import_jur_exemptions` | Upsert jurisdiction/exemption rows from a pre-normalised TSV (`--path`, `--tax-year`) |
| `import_tax_unit_rates` | Upsert per-unit adopted tax rates from TSV (`--path`, `--tax-year`) |

Brazos (`counties/brazos/management/commands/`):

| Command | Purpose |
|---|---|
| `load_brazos_cad` | Download + extract + ingest the certified BCAD archive |
| `load_brazos_gis` | GIS coordinates from the BCAD parcel shapefile (run **after** `load_brazos_cad`) |
| `import_brazos_tax_rates` | Per-entity adopted tax rates |
| `import_brazos_assessment_history` | **Multi-year assessed/appraised/market value history** (`--start-year`, `--end-year`); downloads each year's own certified archive from BCAD's decade-deep portal — no diffing, each year already carries its own values |
| `validate_brazos_against_source` | Cross-check ingested rows against the source files |

### Admin (`counties/harris/admin.py`)
Custom `DownloadRecordAdmin` with an ETL pipeline panel at `/admin/data/downloadrecord/` (the URL
still uses the pinned `data` app label). Exposes GIS/building import triggers and task-status polling.

### URLs (`taxprotest/urls.py`)
Both counties mount the same route set from `counties.common.urls`:

| URL | URL name | Purpose |
|---|---|---|
| `/` | `index` | Harris property search |
| `/export/` | `export_csv` | CSV export of Harris search results |
| `/similar/<key>/` | `similar_properties` | Comparables + protest recommendation |
| `/protest/<key>/` | `protest_analysis` | ARB evidence report |
| `/protest/<key>/export/` | `protest_analysis_export` | CSV of protest comps + tax impact |
| `/protest/<key>/pdf/` | `protest_analysis_pdf` | PDF evidence report |
| `/brazos/…` | `brazos_*` | The identical six routes for Brazos |
| `/about/` | `about` | About page |
| `/healthz/` | `healthz` | Liveness probe |
| `/readiness/` | `readiness` | Readiness probe |
| `/admin/` | — | Django admin |

---

## Similarity Algorithm

`counties/harris/similarity.py` — `find_similar_properties(account_number, max_distance_miles=10.0, max_results=50, min_score=30.0)` → `List[Dict]`
(`counties/brazos/similarity.py` mirrors the structure against BCAD's models.)

**Distance** is a filter only — candidates beyond `max_distance_miles` are excluded before scoring. Distance does not affect the score.

**Residential weights** (`RESIDENTIAL_WEIGHTS`):

| Factor | Weight |
|---|---|
| Living area | 24% |
| Bedrooms | 14% |
| Bathrooms | 12% |
| Land size | 10% |
| Quality | 10% |
| Age | 8% |
| Condition | 6% |
| Stories | 4% |
| Building character | 4% |
| Extra features | 4% |

**Land-only** properties use `LAND_ONLY_WEIGHTS` (land_size 80%, features 10%, distance 10%).

**Score labels:** Best match (≥84) · Highly similar (≥70) · Good match (≥52) · OK match (≥36) · Broad match (<36)

---

## Developer Workflows

**Always use Docker Compose.** Never run Django or Celery directly with local Python.

```bash
# Start everything
docker compose up --build

# Start without Celery (faster for UI work)
docker compose up web postgres redis

# Run full Harris import
docker compose exec web python manage.py import_all_data

# Run the Brazos ETL (separate compose profile)
docker compose --profile etl up etl

# Validate imported data
docker compose exec web python manage.py validate_data

# Shell
docker compose exec web python manage.py shell

# Migrations
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate

# Create admin user
docker compose exec web python manage.py createsuperuser

# Logs
docker compose logs -f web
docker compose logs -f worker
docker compose logs -f beat
```

---

## Running Tests

```bash
# Full suite (pytest via the dev container — this is what `make test` runs)
docker compose run --rm taxprotest-dev pytest -q

# Single module
docker compose run --rm taxprotest-dev pytest counties/harris/tests/test_tax_impact.py

# Single test
docker compose run --rm taxprotest-dev pytest \
  counties/harris/tests/test_similarity_scoring.py::SimilarityScoringTest::test_score_label
```

Tests live beside the code they cover:

- `counties/common/tests/` — the cross-county invariants: every county exposes the same routes, the
  shared equity maths, and Brazos's newly shared pages
- `counties/harris/tests/` — Harris models, ETL, similarity, tax impact, admin, runtime paths, and the
  Harris rendering of the shared pages (`test_views.py`)
- `counties/brazos/tests/` — BCAD parsers, loaders, similarity, and the Brazos rendering of the shared pages
- `taxprotest/tests/` — site-wide views only (about, health, readiness)

---

## Code Quality

Pre-commit hooks (Black, Ruff, EOF/whitespace fixers). Install once:

```bash
pip install -r requirements.txt
pre-commit install
```

Run manually:

```bash
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev black --check .
docker compose run --rm taxprotest-dev mypy taxprotest counties
```

---

## Templates

All templates live in the top-level `templates/` directory — not inside any app. The three property
pages are shared: one template renders every county, driven by the `CountyProfile` and the
`Subject` / `Comp` records its adapter returns.

```
templates/
├── base.html                          # Tailwind layout, navbar, footer
├── about.html
├── counties/
│   ├── index.html                     # Property search (all counties)
│   ├── similar_properties.html        # Comparables + protest recommendation
│   ├── protest_analysis.html          # ARB evidence report (equity + tax impact)
│   └── partials/
│       ├── _cell.html                 # One table cell, rendered from a Column spec
│       ├── _score_cell.html           # Similarity badge + score breakdown
│       └── _assessment_history.html   # Trend chart + per-year table
├── includes/
│   ├── navbar.html
│   └── footer.html
├── components/
│   └── sort_header.html
└── admin/
    └── data/
        └── downloadrecord/            # Custom admin ETL pipeline templates
```

New templates must extend `base.html`. Use Tailwind utility classes for all UI. **Do not add a
county-specific copy of a shared page** — extend `CountyProfile` (a new `Column` format, a
`DetailRow`, a `search_notice`) so every county benefits.

---

## Background Tasks (Celery)

Configured in `taxprotest/celery.py`. Redis is the broker. The beat schedule database is written to
`taxprotest/var/celerybeat-schedule` (override with `CELERY_BEAT_SCHEDULE_FILENAME`).

| Task | Schedule | Function |
|---|---|---|
| Building data import | 2nd Tuesday of month, 2 AM Central | `counties.harris.tasks_new.run_etl_pipeline` (`scope="building-only"`) |
| GIS import | January 15, 3 AM Central | `counties.harris.tasks_new.run_etl_pipeline` (`scope="gis-only"`) |

Task names are module paths — renaming or moving a task module changes its Celery name, so update
`beat_schedule` alongside any such move.

---

## Static Files

`staticfiles/` is **not committed to git** — it is generated at container build time by `collectstatic`. Do not add it back to version control.

---

## Data Sources

**Harris County (HCAD)** — https://download.hcad.org/data/

| File | Contents |
|---|---|
| `Real_acct_owner.txt` | Property records |
| `Real_building_land.zip` | Building details and features |
| `Parcels.zip` | GIS shapefiles (~800MB) |
| `Real_jur_exempt.zip` | Jurisdiction values, exemptions, and tax rates (~110MB) |

Downloaded at build time via `scripts/build_time_download.py` into `counties/harris/var/downloads/`,
extracted to `counties/harris/var/extracted/`.

**Brazos County (BCAD)** — certified PACS export, fetched by `load_brazos_cad` into
`counties/brazos/var/downloads/` and extracted to `counties/brazos/var/extracted/<year>/`. The files
are fixed-width with no header row; offsets are pinned in `counties/brazos/parsers/pacs.py`. BCAD's
download portal keeps roughly a decade of past certified years available (confirmed 2016–2025 as of
2026-08); `import_brazos_assessment_history` downloads each target year's own archive rather than
diffing consecutive years — nothing in the export carries a prior-year value column.

---

## Documentation

| File | Contents |
|---|---|
| `README.md` | Overview, features, quick start, project layout, adding a county |
| `docs/guides/SETUP.md` | Installation, Docker services, production deployment |
| `docs/guides/DEPLOYMENT.md` | Production deployment walkthrough |
| `docs/guides/DATABASE.md` | ETL processes, import commands, DB management |
| `docs/guides/GIS.md` | GIS data handling, coordinate storage, similarity distance |
| `docs/SIMILARITY_SCORING.md` | Similarity algorithm deep-dive |
| `docs/ETL_PIPELINE.md` | ETL pipeline architecture |
| `docs/REVERSE_PROXY.md` | Reverse proxy / production deployment notes |

---

## Conventions

- **County pages come from `counties/common/`.** Do not add per-county views or templates for search,
  comparables, or the protest report; extend the adapter/profile instead. Site-wide pages (about,
  health probes) go in `taxprotest/views.py`.
- Runtime data belongs in `counties/<slug>/var/`, resolved through `taxprotest/runtime_paths.py` —
  never hardcode a path or write to the project root.
- Use environment variables for all secrets/configuration — never hardcode.
- `is_residential=True` and `is_data_ready=True` are the contract for queryable Harris properties.
- All Harris ETL helper logic goes in `counties/harris/etl.py` or `counties/harris/residential.py`,
  not inline in management commands.
- Celery tasks import from `counties.harris.tasks_new`.
- Tax impact calculations require `TaxUnitRate` and `PropertyJurisdictionExemption` rows before the
  protest analysis views show meaningful results; missing data degrades gracefully to
  `completeness="missing"`. For Harris, populate both with `import_hcad_jur_exempt --tax-year YYYY`
  (from `Real_jur_exempt.zip`); Brazos gets them from `load_brazos_cad`. The generic
  `import_jur_exemptions` / `import_tax_unit_rates` commands take pre-normalised TSVs and cannot
  read HCAD's raw files.
- **`AssessmentHistory.cap_account` means different things per county and neither is a clean boolean.**
  Harris's `Cap_acct` is HCAD's own `Y`/`N`/`Pending` flag; Brazos has no such field, so it's derived
  as `"Y"` when `appraised_value > assessed_value` for that year (see
  `import_brazos_assessment_history`'s docstring for why that delta is the cap and not an
  entity-specific exemption). `counties/common/cap_status.py`'s `evaluate_cap_status` only decodes the
  flag into `homestead`/`circuit_breaker` for counties in `COUNTIES_WITH_TYPED_CAP_FLAG` (today: Harris
  alone) — any other county gets an honest `cap_type="unknown"`/`status="unknown"` rather than a guess,
  since a derived flag doesn't say which cap applied. Still open, and out of scope for that function:
  issue #14 (the homestead-vs-circuit-breaker rule itself ignores tax year — the 20% circuit breaker is
  a 2023 SB2 provision applied here to pre-2023 rows too — and an unmodelled ~$5M value ceiling).
- **Tax rates are stored fractional** (`0.00878300`), not per $100 as HCAD and most districts publish
  them (`0.878300`) — `calculate_tax_impact` multiplies a taxable value by `adopted_rate` directly.
- **`PropertyJurisdictionExemption` stores the gross value on its base row** (`exemption_code=""`)
  with the reduction on a companion row, because `calculate_tax_impact` subtracts exemptions itself.
  Storing an already-net taxable value there double-counts.
- `AssessmentHistory`, `TaxUnitRate`, and `PropertyJurisdictionExemption` are shared tables scoped by
  a `county` column. Always pass `county=` when querying them.

---

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (`PorkChopExpress86/TaxProtest-Django`), via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
