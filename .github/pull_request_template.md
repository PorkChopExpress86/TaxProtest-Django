## Summary

What changed?

## Type of change

- [ ] Web/UI
- [ ] Comparables engine
- [ ] Ingestion
- [ ] Database/PostGIS
- [ ] Docker/dev workflow
- [ ] Tests only
- [ ] Docs only

## Docker validation

- [ ] `docker compose build`
- [ ] `docker compose up -d postgres`
- [ ] `docker compose run --rm taxprotest-dev ruff check .`
- [ ] `docker compose run --rm taxprotest-dev black --check .`
- [ ] `docker compose run --rm taxprotest-dev mypy taxprotest data`
- [ ] `docker compose run --rm taxprotest-dev pytest -q`
- [ ] `docker compose run --rm ingest` if ingestion/database code changed
- [ ] `docker compose run --rm refresh` if refresh orchestration changed

## Performance impact

Describe any expected impact on:
- startup time
- ingestion time
- query time
- memory usage
- database size

## Data compatibility

- [ ] SQLite fallback still works, or this is intentionally Postgres-only
- [ ] Postgres path still works
- [ ] No full HCAD dataset required for tests
- [ ] No generated data committed
