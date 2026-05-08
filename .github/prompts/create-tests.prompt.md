---
name: create-tests
description: Add focused pytest coverage for a TaxProtest module, bug, or feature.
---

Use pytest with small fixtures and keep tests fast.

Guidelines:

- Do not require full HCAD data downloads.
- Cover SQLite fallback where practical.
- If testing Postgres-specific code, isolate it and skip when unavailable.
- Prefer testing pure functions under `src/taxprotest/` over route-level tests unless route behavior is the feature.
- Run tests inside Docker only.

Validation:

```bash
docker compose run --rm taxprotest-dev pytest -q
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev mypy taxprotest data
```
