.DEFAULT_GOAL := help
SHELL := /bin/bash
BACKEND := backend
FRONTEND := frontend
NPM := npm --prefix $(FRONTEND)
UV := uv --directory $(BACKEND)
COMPOSE := docker compose

COMPOSE_PROD := docker compose -f docker-compose.prod.yml

.PHONY: help env install lint format typecheck test test-unit check up down restart logs ps shell \
	migrate migrate-down revision bot-logs openapi \
	ui-install ui-types ui-lint ui-typecheck ui-test ui-build ui-dev ui-check clean \
	preflight tls-init renew prod-build prod-up prod-down prod-restart prod-ps prod-logs \
	prod-migrate backup restore

help: ## Show the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from the template when it does not exist yet
	@test -f .env || (cp .env.example .env && echo ".env created — fill in the secrets")

install: ## Sync the locked backend environment (including dev tools)
	$(UV) sync --frozen

lint: ## Run ruff lint and formatting checks
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Auto-fix lint findings and format the code
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## Run mypy in strict mode
	$(UV) run mypy

test: ## Run every test (needs TEST_DATABASE_DSN for the integration suite)
	$(UV) run pytest --cov=app --cov-report=term-missing

test-unit: ## Run only the tests that need no database
	$(UV) run pytest --cov=app --cov-report=term-missing -p no:cacheprovider \
		--ignore=tests/test_repository_products.py \
		--ignore=tests/test_repository_purchases.py \
		--ignore=tests/test_repository_stats.py \
		--ignore=tests/test_migrations.py \
		--ignore=tests/test_contracts.py

migrate: ## Apply every pending migration
	$(COMPOSE) run --rm migrations alembic upgrade head

migrate-down: ## Roll back the most recent migration
	$(COMPOSE) run --rm migrations alembic downgrade -1

revision: ## Autogenerate a migration: make revision m="add coupons"
	$(COMPOSE) run --rm migrations alembic revision --autogenerate -m "$(m)"

openapi: ## Refresh backend/openapi.json (source of the frontend types)
	$(UV) run python scripts/export_openapi.py

ui-install: ## Install the admin panel dependencies
	$(NPM) install

ui-types: openapi ## Regenerate the TypeScript types from OpenAPI
	$(NPM) run api:types

ui-lint: ## Lint the admin panel
	$(NPM) run lint

ui-typecheck: ## Type-check the admin panel
	$(NPM) run typecheck

ui-test: ## Run the admin panel tests
	$(NPM) run test

ui-build: ## Type-check and build the admin panel
	$(NPM) run build

ui-dev: ## Start the admin panel dev server
	$(NPM) run dev

ui-check: ui-lint ui-typecheck ui-test ui-build ## Everything CI runs for the panel

check: lint typecheck test ## Everything CI runs for the backend

up: env ## Start the development stack
	$(COMPOSE) up -d --build

down: ## Stop the stack and keep the volumes
	$(COMPOSE) down

restart: ## Recreate the api container
	$(COMPOSE) up -d --build --force-recreate api

logs: ## Follow the api logs
	$(COMPOSE) logs -f api

bot-logs: ## Follow the bot logs
	$(COMPOSE) logs -f bot

ps: ## Show container status
	$(COMPOSE) ps

shell: ## Open a shell inside the api container
	$(COMPOSE) exec api /bin/bash

# --------------------------------- Production -------------------------------- #

preflight: ## Check .env for everything that would break a production deploy
	bash scripts/preflight.sh

tls-init: ## Issue the first TLS certificate (run before the first prod-up)
	bash scripts/tls-init.sh

renew: ## Renew the certificate and reload nginx when it changed (cron target)
	bash scripts/renew-certs.sh

prod-build: ## Build the production images
	$(COMPOSE_PROD) build --pull

prod-up: ## Start the production stack
	$(COMPOSE_PROD) up -d

prod-down: ## Stop the production stack and keep the volumes
	$(COMPOSE_PROD) down

prod-restart: ## Rebuild and recreate the application containers
	$(COMPOSE_PROD) up -d --build api bot nginx

prod-ps: ## Show production container status
	$(COMPOSE_PROD) ps

prod-logs: ## Follow every production log
	$(COMPOSE_PROD) logs -f --tail 100

prod-migrate: ## Apply pending migrations in production
	$(COMPOSE_PROD) run --rm migrations alembic upgrade head

backup: ## Dump the production database into ./backups
	bash scripts/backup.sh

restore: ## Restore a dump: make restore f=backups/tgshop-20260130-021500Z.dump
	bash scripts/restore.sh "$(f)"

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache \
		$(BACKEND)/.coverage $(BACKEND)/htmlcov
