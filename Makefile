# Project X — common tasks. Run `make help` for the list.

SHELL := /bin/bash
BACKEND := cd backend &&
WEB := cd web &&

.PHONY: help setup db-start db-stop db-setup db-reset migrate api worker seed test lint fmt typecheck \
        web-install web-dev web-build web-check openapi check

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

setup: db-start db-setup migrate web-install  ## First-time setup: database, migrations, web dependencies
	$(BACKEND) uv sync

db-start:  ## Start local PostgreSQL 18
	./infra/scripts/dev-db.sh start

db-stop:  ## Stop local PostgreSQL
	./infra/scripts/dev-db.sh stop

db-setup:  ## Create the app role, databases and extensions
	./infra/scripts/dev-db.sh setup

db-reset:  ## Drop and recreate the local databases
	./infra/scripts/dev-db.sh reset

migrate:  ## Apply database migrations
	$(BACKEND) uv run alembic upgrade head

api:  ## Run the API with auto-reload on http://localhost:8000
	$(BACKEND) uv run projectx api --reload

worker:  ## Run background jobs and the scheduler
	$(BACKEND) uv run projectx worker

seed:  ## Create a practice company with sample data
	$(BACKEND) uv run projectx seed-demo

test:  ## Run backend tests
	$(BACKEND) uv run pytest -q

lint:  ## Lint and format-check the backend
	$(BACKEND) uv run ruff check src tests migrations && uv run ruff format --check src tests migrations

fmt:  ## Format the backend
	$(BACKEND) uv run ruff check --fix src tests migrations && uv run ruff format src tests migrations

typecheck:  ## Strict type check of the backend
	$(BACKEND) uv run mypy src

openapi:  ## Regenerate the web client's API types from the backend
	$(BACKEND) uv run projectx openapi --output ../web/src/api/openapi.json
	$(WEB) pnpm run gen:api

web-install:  ## Install web dependencies
	$(WEB) pnpm install

web-dev:  ## Run the cockpit on http://localhost:5173 (proxies the API)
	$(WEB) pnpm dev

web-build:  ## Build the cockpit for production (served by the API from web/dist)
	$(WEB) pnpm build

web-check:  ## Lint, type-check and test the web app
	$(WEB) pnpm run check

check: lint typecheck test web-check  ## Everything CI runs
