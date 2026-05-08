---
name: add-comparable-feature
description: Add or modify comparable-property matching, scoring, filters, stats, or exports.
---

Inspect `src/taxprotest/comparables/` first.

Guidelines:

- Keep Flask routes thin.
- Preserve SQLite fallback.
- Prefer PostGIS spatial filtering when `TAXPROTEST_DATABASE_URL` is set.
- Add or update tests in `tests/`.
- Avoid tests that require full HCAD data.
- Use small fixtures.
- Validate inside Docker only.

Validation:

```bash
docker compose run --rm taxprotest-dev pytest -q
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev black --check .
docker compose run --rm taxprotest-dev mypy taxprotest data
```
