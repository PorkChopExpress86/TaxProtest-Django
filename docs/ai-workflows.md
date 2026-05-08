# AI Workflows for TaxProtest

This project is **Docker-first** for development and validation.

Do not assume host Python tooling is installed. Use Docker Compose for linting, tests, ingestion, refresh, and app workflows.

## Copilot prompt files

Prompt files live in `.github/prompts/` and help keep changes targeted, safe, and repeatable.

Use:

- `optimize-ingestion` for HCAD download/extract/import/COPY/hash/refresh/profiling performance work.
- `add-comparable-feature` for matching, scoring, filters, stats, and exports in comparables.
- `docker-dev-workflow` for Dockerfile, Compose, env variables, devcontainer, and local workflow improvements.
- `create-tests` for focused pytest coverage with small fixtures.

## Recommended agent roles

### Data Ingestion Agent
- Scope: ETL pipeline, extraction, hashing, refresh orchestration, profiling.
- Guardrails: preserve hash-based skipping, stream large files, keep profiling behind `TAXPROTEST_PROFILE_LOAD=1`, avoid committing generated artifacts.

### Comparables Engine Agent
- Scope: comparable search, scoring, distance filtering, exports.
- Guardrails: keep routes thin, implement business logic in services/engine modules, add/update tests or benchmark notes.

### Web UX Agent
- Scope: UI templates, interaction flow, usability changes.
- Guardrails: keep behavior testable, avoid coupling view code to heavy business logic, maintain Docker-based validation.

### DevOps/Quality Agent
- Scope: Docker Compose workflows, lint/type/test gates, development tooling.
- Guardrails: prefer `.env`/`.env.example`, avoid host-only setup steps, keep commands copy/paste friendly.

## Standard Docker validation commands

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
