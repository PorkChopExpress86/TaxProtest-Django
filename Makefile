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
	SKIP_DATA_DOWNLOAD=1 podman compose build

up:
	podman compose up -d postgres web

dev:
	podman compose up -d postgres taxprotest-dev

down:
	podman compose down

test:
	podman compose run --rm taxprotest-dev pytest -q

lint:
	podman compose run --rm taxprotest-dev ruff check .
	podman compose run --rm taxprotest-dev black --check .

fmt:
	podman compose run --rm taxprotest-dev ruff check --fix .
	podman compose run --rm taxprotest-dev black .

type:
	podman compose run --rm taxprotest-dev mypy taxprotest data

ingest:
	podman compose up -d postgres
	podman compose run --rm ingest

refresh:
	podman compose run --rm refresh

shell:
	podman compose run --rm taxprotest-dev bash

logs:
	podman compose logs -f taxprotest-dev

clean:
	podman compose down --remove-orphans
