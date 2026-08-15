# AI Workflows for TaxProtest

This project is **Docker-first** for development and validation.

Do not assume host Python tooling is installed. Use Docker Compose for linting, tests, ingestion, refresh, and app workflows.

## Recommended Agent Roles

### Data Ingestion Agent
- **Scope:** ETL pipeline, extraction, row readers, COPY fast loaders, refresh orchestration.
- **Guardrails:** Stream large files, keep batch sizes reasonable (5,000–10,000), avoid committing generated artifacts.

### Comparables Engine Agent
- **Scope:** Comparable search, similarity math, scoring curves, distance filtering, exports.
- **Guardrails:** Keep views thin, implement pure business logic in `counties/common/` or county scoring modules, maintain full test coverage.

### Web UX Agent
- **Scope:** UI templates, interaction flow, usability changes across shared county views.
- **Guardrails:** Keep behavior testable, avoid coupling view code to heavy database ETL logic, maintain Docker-based validation.

### DevOps & Quality Agent
- **Scope:** Docker Compose workflows, lint/type/test gates, deployment automation (`scripts/deploy.sh`).
- **Guardrails:** Prefer `.env`/`.env.example`, avoid host-only setup steps, ensure migrations and static files build cleanly.

## Standard Validation Commands

```bash
# Code formatting and linting
pre-commit run --all-files

# Type checking
mypy

# Full Django test suite
docker compose exec web python manage.py test

# Targeted county test suites
docker compose exec web python manage.py test counties.harris
docker compose exec web python manage.py test counties.brazos
docker compose exec web python manage.py test counties.common
```
