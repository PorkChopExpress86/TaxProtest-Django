---
name: docker-dev-workflow
description: Fix or improve Docker, Docker Compose, dev containers, environment variables, and local developer setup.
---

Inspect these files before editing:

- `Dockerfile`
- `docker-compose.yml`
- `Makefile`
- `README.md`
- `.env.example` (if present)

Guidelines:

- Keep app, dev app, postgres, ingest, refresh, and django workflows separate.
- Avoid hardcoding secrets beyond local defaults.
- Prefer `.env` / `.env.example` for local configuration.
- Do not introduce host Python setup steps.
- Keep commands copy/paste friendly.
- Validation must use Docker Compose only.
