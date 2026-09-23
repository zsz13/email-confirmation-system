# Entry points for this repository, so the test layers are obvious from the root.
#
#   make test               fast backend unit and API tests
#   make test-integration   the Docker integration test (real MySQL, stubbed Mailjet)
#   make test-e2e           the real-email test; opt in with RUN_REAL_EMAIL_E2E=1
#   make test-all           test + test-integration, and nothing that leaves the machine
#
# Every target is a thin wrapper around the tool that owns the job (uv, pnpm, docker
# compose, the scripts in scripts/); none of them reimplements what it calls.

.DEFAULT_GOAL := help
.PHONY: help test test-integration test-e2e test-all lint format-check build docker-up docker-down

help:  ## List the targets
	@grep -hE '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-18s %s\n", $$1, $$2}'

test:  ## Backend unit and API tests: SQLite, a fake Mailjet, no Docker, seconds
	cd backend && uv run pytest

test-integration:  ## Docker integration test: real images, MySQL, migrations, stubbed Mailjet, no real email
	./scripts/integration-test.sh

# The script owns the opt-in check and fails without it; this only passes the flag through,
# so `make test-e2e` and the script itself cannot disagree about what counts as opting in.
test-e2e:  ## Real Mailjet end-to-end test; sends one email. RUN_REAL_EMAIL_E2E=1 make test-e2e
	@RUN_REAL_EMAIL_E2E="$(RUN_REAL_EMAIL_E2E)" ./scripts/e2e-real-email.sh

test-all:  ## test + test-integration. Deliberately excludes test-e2e: nothing leaves the machine
	$(MAKE) test
	$(MAKE) test-integration

lint:  ## Ruff and Pyright for the backend, ESLint for the frontend
	cd backend && uv run ruff check . && uv run pyright
	cd frontend && pnpm lint

format-check:  ## Formatting check only; `uv run ruff format .` and `pnpm format` apply it
	cd backend && uv run ruff format --check .
	cd frontend && pnpm format:check

build:  ## Build the Docker images
	docker compose build

docker-up:  ## Start the whole stack in the background and wait until it is healthy
	docker compose up --build --detach --wait

docker-down:  ## Stop the stack and keep the database volume (`docker compose down -v` drops it)
	docker compose down
