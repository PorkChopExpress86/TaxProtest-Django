# Copilot Instructions for TaxProtest

## Project Overview
- This is a Python 3.13 property-tax research app for Harris County appraisal data.
- Canonical package code lives under `src/taxprotest/`.
- Flask app logic should use the existing app factory/package structure.
- Comparables logic belongs under `src/taxprotest/comparables/`.
- Ingestion scripts live in `scripts/` or existing top-level legacy step scripts.

## Container-only development rule

This project is Docker-first / Docker-only for development. Do not assume Python, pip, pytest, ruff, mypy, Postgres, GDAL, or other dependencies are installed on the host machine.

Use Docker Compose for development, linting, typing, tests, ingestion, and refresh orchestration.

Preferred commands:

```bash
docker compose build
docker compose up -d postgres
docker compose up -d taxprotest-dev
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev black --check .
docker compose run --rm taxprotest-dev mypy taxprotest data
docker compose run --rm taxprotest-dev pytest -q
docker compose run --rm ingest
docker compose run --rm refresh
```

## Coding and Architecture Guardrails
- Avoid creating duplicate root packages.
- Prefer Postgres/PostGIS where available, but preserve SQLite fallback unless the task explicitly removes it.
- Keep Flask routes thin; put business logic in service/engine modules.
- Large HCAD files must be streamed/chunked.
- Do not load full HCAD files into memory without clear justification.
- Do not commit downloaded data, generated exports, SQLite databases, hash artifacts, logs, or profiling outputs.
- New or modified functions should include type hints.

## Ingestion and Comparables Expectations
- Any ingestion change must preserve hash-based skipping and profiling behavior.
- Keep profiling gated by `TAXPROTEST_PROFILE_LOAD=1`.
- Any comparables change should include tests or a benchmark note.
- Prefer small fixtures in tests; avoid requiring full HCAD datasets for routine validation.

## Validation Standard
All validation commands must run in Docker Compose.