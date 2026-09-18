# TaxProtest-Django agent guide

TaxProtest-Django is a Django application for Texas property-tax protest analysis. Harris and
Brazos use county-owned ingestion and data models behind one shared search, comparables, and protest
report surface.

## Start with the relevant source of truth

- Architecture, domain language, or ETL policy: read `CONTEXT.md`, the applicable accepted records
  in `docs/adr/`, and `docs/agents/domain.md`. An ADR records a decision, not proof that its
  implementation is complete. Surface conflicts and leave undocumented policy unresolved.
- Setup, services, data imports, or project layout: use `README.md` and the matching guide under
  `docs/guides/`; verify commands against `docker-compose.yml` or `Makefile` before relying on prose.
- Similarity behavior: use `docs/guides/SIMILARITY.md` and the county implementation/tests.
- HCAD source fields: use `docs/hcad_docs/HCAD_DATA_REFERENCE.md` and the importer tests.
- Issue work or triage: follow `docs/agents/issue-tracker.md` and
  `docs/agents/triage-labels.md` before making GitHub changes.

Read the relevant implementation and adjacent tests before editing. Check `git status` first and
preserve unrelated work, runtime data, databases, and secrets.

## Load-bearing architecture

- County ETL stays county-owned under `counties/harris/` or `counties/brazos/`. Do not introduce a
  generic cross-county ETL framework.
- The county-neutral web surface stays in `counties/common/`. Add county web behavior through a
  `CountyAdapter` and `CountyProfile`, not county-specific search, comparables, report views, or
  copies of shared templates.
- Django app labels are pinned: Harris is `data`, Brazos is `brazos_cad`, and common template-tag
  discovery is `counties_common`. Preserve table names, migration ownership, content types, and
  historical references such as `data.PropertyRecord`.
- `AssessmentHistory`, `TaxUnitRate`, and `PropertyJurisdictionExemption` are shared tables whose
  models live in `counties/common/tax_models.py` but whose `Meta.app_label` remains `data`. Scope
  every read and write with `county=`; migrations remain in `counties/harris/migrations/`.
- Runtime artifacts belong under `counties/<slug>/var/` through
  `taxprotest/runtime_paths.py`; Celery beat state belongs under `taxprotest/var/`. Keep downloads,
  extracts, logs, reports, generated databases, and `staticfiles/` out of source control.

## Data invariants

- Harris queryable records satisfy both `is_residential=True` and `is_data_ready=True`.
- Harris imports cross `run_harris_import(HarrisImportRequest)` in
  `counties/harris/etl_pipeline/`; management commands and Celery tasks are adapters. Keep Celery
  task names and `beat_schedule` synchronized when moving task code.
- `refresh_brazos_annual` is the only year-matched certified CAD-plus-GIS publication path.
  `enrich_brazos_coordinates` is a separately thresholded, coordinate-only fallback that preserves
  the actual GIS source year.
- Tax rates are stored as fractions (`0.00878300` means `0.878300%`). A
  `PropertyJurisdictionExemption` base row stores gross value; companion exemption rows store the
  reductions. Do not pre-net the base value.
- `AssessmentHistory.cap_account` is county-specific source data, not a portable boolean. Use
  `counties/common/cap_status.py`; do not infer a cap type for counties without a typed source flag.

## Implementation workflow

1. Inspect the applicable guidance, code, tests, and current Git diff.
2. For a bug, reproduce it with the narrowest meaningful test or check.
3. Make the smallest complete change at the owning module or adapter seam.
4. Run the focused test first, then broader checks proportional to the affected contract.
5. Fix failures and rerun the failed checks. Report any genuine blocker or unverified risk.

Use Docker Compose for Django, tests, linting, formatting, types, migrations, and Celery. Do not use
the host Python environment for project execution.

```bash
# Focused or full tests
docker compose run --rm taxprotest-dev pytest path/to/test_module.py -q
docker compose run --rm taxprotest-dev pytest -q

# Quality gates
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev black --check .
docker compose run --rm taxprotest-dev mypy taxprotest counties
docker compose run --rm taxprotest-dev python manage.py makemigrations --check --dry-run
```

Use the nearest tests: `counties/common/tests/` for cross-county contracts,
`counties/<slug>/tests/` for county behavior, and `taxprotest/tests/` for site-wide behavior.
Run `git diff --check` for every change. Do not claim browser, service, or full-suite validation that
was not actually run.

## Presentation and delivery

- Shared templates live under `templates/counties/`, extend `templates/base.html`, and use Tailwind
  utilities. Preserve the identical county route set enforced by
  `counties/common/tests/test_shared_pages.py`.
- Use environment variables and the existing runtime-path helpers for configuration. Never hardcode
  credentials or production data.
- Keep edits scoped. Commit, push, close issues, or change external state only when the request
  explicitly includes that action.
- Finish by reporting the files changed, checks run with observed results, and anything still
  unverified.

## Agent skills

### Issue tracker

Issues and specs live in this repository's GitHub Issues. Follow `docs/agents/issue-tracker.md`.

### Triage labels

Use the default triage labels mapped in `docs/agents/triage-labels.md` when triaging issues.

### Domain docs

Single-context layout: root `CONTEXT.md` and `docs/adr/`. Follow `docs/agents/domain.md`
when exploring domain terminology or architectural decisions.
