# Convenience targets. Everything also works with the underlying commands (see README).
.PHONY: up down logs reset test test-backend test-web lint seed contracts

up:            ## Build and start the full stack
	docker compose up --build

down:          ## Stop the stack
	docker compose down

reset:         ## Stop the stack and delete database/redis volumes
	docker compose down --volumes

logs:
	docker compose logs -f api worker

seed:          ## Re-run the (idempotent) demo seed
	docker compose run --rm migrate

test: test-backend test-web

test-backend:  ## Backend tests inside the backend image against the compose PostgreSQL
	docker compose run --rm api pytest

test-web:
	npm run test

lint:
	docker compose run --rm api sh -c "ruff check . && ruff format --check . && mypy"
	npm run lint && npm run typecheck

contracts:     ## Regenerate JSON Schemas, OpenAPI and TypeScript types
	cd apps/backend && python -m careos.cli export-contracts
	npm run contracts:generate
