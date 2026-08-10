.PHONY: help build up dev down test lint fmt type ingest refresh shell logs clean

help:
	@echo "Docker-first commands:"
	@echo "  make build     - build Docker images"
	@echo "  make up        - start postgres + production app"
	@echo "  make dev       - start postgres + dev app"
	@echo "  make down      - stop compose stack"
	@echo "  make test      - run pytest inside dev container"
	@echo "  make lint      - run ruff + black check inside dev container"
	@echo "  make fmt       - auto-format inside dev container"
	@echo "  make type      - run mypy inside dev container"
	@echo "  make ingest    - run Postgres ingestion container"
	@echo "  make refresh   - run refresh container"
	@echo "  make shell     - open shell in dev container"
	@echo "  make logs      - follow dev app logs"

build:
	SKIP_DATA_DOWNLOAD=1 docker compose build

up:
	docker compose up -d postgres web

dev:
	docker compose up -d postgres taxprotest-dev

down:
	docker compose down

test:
	docker compose run --rm taxprotest-dev pytest -q

lint:
	docker compose run --rm taxprotest-dev ruff check .
	docker compose run --rm taxprotest-dev black --check .

fmt:
	docker compose run --rm taxprotest-dev ruff check --fix .
	docker compose run --rm taxprotest-dev black .

type:
	docker compose run --rm taxprotest-dev mypy taxprotest data

ingest:
	docker compose up -d postgres
	docker compose run --rm ingest

refresh:
	docker compose run --rm refresh

shell:
	docker compose run --rm taxprotest-dev bash

logs:
	docker compose logs -f taxprotest-dev

clean:
	docker compose down --remove-orphans
