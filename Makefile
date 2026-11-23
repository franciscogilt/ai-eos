# AI-EOS developer commands. Requires: docker compose v2, uv, Python 3.12+.
SHELL := /bin/bash
.DEFAULT_GOAL := help
ENV_FILE ?= .env

.PHONY: help install up up-api down reset migrate test test-integration check fmt audit dev-keys run-api create-org token publish-agents run-worker-agents run-worker-gateway test-temporal eval eval-qa sandbox-image test-sandbox

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

install: ## Install runtime and dev dependencies
	uv sync

up: ## Start local infrastructure
	docker compose --env-file $(ENV_FILE) up -d --wait postgres temporal s3 jaeger
	docker compose --env-file $(ENV_FILE) run --rm s3-init

up-api: ## Build and start API + both workers (needs `make dev-keys`, `make migrate`, `make publish-agents`)
	docker compose --env-file $(ENV_FILE) --profile app build api
	docker compose --env-file $(ENV_FILE) --profile app up -d --wait api worker-agents worker-gateway

down: ## Stop local infrastructure (keeps volumes)
	docker compose --env-file $(ENV_FILE) --profile app down

reset: ## Stop and DELETE local volumes (local data only)
	docker compose --env-file $(ENV_FILE) down -v

migrate: ## Apply migrations as the owner role, then grant the runtime role
	set -a; source $(ENV_FILE); set +a; uv run alembic upgrade head
	docker compose --env-file $(ENV_FILE) exec -T postgres psql -U postgres -d aieos -v ON_ERROR_STOP=1 -c "GRANT aieos_app TO aieos_api"

test: ## Unit tests
	uv run pytest tests/unit

test-integration: ## Database tests (needs `make up`); uses the owner role to create a throwaway database
	set -a; source $(ENV_FILE); set +a; \
	ADMIN_DATABASE_URL="postgresql://aieos_owner:$${AIEOS_OWNER_PASSWORD}@127.0.0.1:5432/aieos" uv run pytest tests/integration

check: ## Lint, format check, type check, module boundaries
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy
	uv run lint-imports

fmt: ## Auto-format
	uv run ruff format src tests
	uv run ruff check --fix src tests

audit: ## Dependency vulnerability scan
	uv run pip-audit

dev-keys: ## LOCAL ONLY: create a dev signing key + JWKS in .dev-keys/
	uv run aieos-admin dev-keys --out .dev-keys

run-api: ## Run the API on the host with auto-loaded .env
	uv run aieos-api

create-org: ## Provision an org: make create-org SLUG=acme NAME="Acme" SUB=dev-owner EMAIL=dev@acme.test
	set -a; source $(ENV_FILE); set +a; uv run aieos-admin create-org --slug "$(SLUG)" --name "$(NAME)" --owner-sub "$(SUB)" --owner-email "$(EMAIL)"

token: ## LOCAL ONLY: print a bearer token: make token SUB=dev-owner EMAIL=dev@acme.test
	set -a; source $(ENV_FILE); set +a; uv run aieos-admin dev-token --sub "$(SUB)" --email "$(EMAIL)"

publish-agents: ## Publish agent contracts as immutable versions (owner role)
	set -a; source $(ENV_FILE); set +a; uv run aieos-admin publish-agents

run-worker-agents: ## Run the agent worker on the host
	uv run aieos-worker agents

run-worker-gateway: ## Run the tool-gateway worker on the host
	uv run aieos-worker gateway

test-temporal: ## Workflow tests on Temporal's test server (downloaded by the SDK on first use)
	AIEOS_REQUIRE_TEMPORAL=1 uv run pytest tests/temporal -m temporal

eval: ## Golden-set eval of the Requirements agent against REAL models (spends up to MAX_USD, default 2)
	set -a; source $(ENV_FILE); set +a; uv run aieos-eval --agent requirements --cases engineering/evals/requirements --max-usd $${MAX_USD:-2}

eval-qa: ## Golden-set eval of the QA/Security agent (scripted platform evidence; real model)
	set -a; source $(ENV_FILE); set +a; uv run aieos-eval --agent qa_security --cases engineering/evals/qa_security --max-usd $${MAX_USD:-2}

sandbox-image: ## Build the code-execution sandbox image
	docker build -f infrastructure/docker/sandbox.Dockerfile -t aieos-sandbox:local .

test-sandbox: sandbox-image ## Real-Docker isolation tests for the sandbox
	AIEOS_REQUIRE_DOCKER=1 uv run pytest tests/sandbox -m docker -v
