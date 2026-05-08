---
name: optimize-ingestion
description: Improve HCAD download, extraction, import, COPY loading, hashing, refresh, or profiling performance.
---

Inspect these ingestion files before editing:

- `setup_complete.py`
- `refresh.py`
- `extract_data.py`
- `scripts/ingest_postgres.py`
- `scripts/init_postgres.sql`
- `load_geo_data.py`
- `step1_download.py`
- `step2_extract.py`
- `step3_import.py`

Rules and guardrails:

- Preserve hash-based skipping.
- Preserve SQLite fallback unless explicitly changing Postgres-only code.
- Use streaming/chunked reads for large files.
- Do not load full HCAD files into memory without justification.
- Keep profiling gated by `TAXPROTEST_PROFILE_LOAD=1`.
- Add small fixture-based tests where practical.
- Validate using Docker Compose commands only.

Validation:

```bash
docker compose build
docker compose up -d postgres
docker compose run --rm ingest
docker compose run --rm taxprotest-dev pytest -q
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev mypy taxprotest data
```

Output summary must include:

- Bottleneck identified
- Change made
- Validation performed
- Expected performance impact