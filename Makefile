.DEFAULT_GOAL := help
SHELL := /bin/bash
BACKEND := backend
UV := uv --directory $(BACKEND)
COMPOSE := docker compose

.PHONY: help env install lint format typecheck test test-unit check up down restart logs ps shell \
	migrate migrate-down revision bot-logs clean

help: ## Show the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

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

check: lint typecheck test ## Everything CI runs

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

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache \
		$(BACKEND)/.coverage $(BACKEND)/htmlcov
