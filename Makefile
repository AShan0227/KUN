# KUN dev convenience targets. Run `make help`.

.PHONY: help install dev test lint format typecheck up down migrate run-cli serve clean preflight readiness dogfood delivery-status

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## install dev deps via uv
	uv sync --extra dev

dev: install  ## alias for install

test:  ## run all tests
	uv run pytest tests/ -q

lint:  ## ruff check + format check
	uv run ruff check kun tests
	uv run ruff format --check kun tests

format:  ## apply ruff format fixes
	uv run ruff check --fix kun tests
	uv run ruff format kun tests

typecheck:  ## mypy (non-blocking)
	uv run mypy kun || true

up:  ## bring up dev infrastructure (postgres/redis/qdrant/nats/minio/otel/grafana)
	docker compose -f docker-compose.dev.yml up -d

down:  ## tear down
	docker compose -f docker-compose.dev.yml down

down-volumes:  ## tear down + wipe volumes
	docker compose -f docker-compose.dev.yml down -v

migrate:  ## apply alembic migrations
	uv run alembic upgrade head

revision:  ## create new alembic migration: make revision m="..."
	uv run alembic revision --autogenerate -m "$(m)"

run-cli:  ## quick CLI smoke: kun run "hello"
	uv run kun run "Say hi to the world"

serve:  ## run FastAPI with autoreload
	uv run kun serve --reload

rules:  ## list watchtower rules
	uv run kun rules

skills:  ## list starter skills
	uv run kun skills

idle-batch:  ## run one idle-batch pass (health_report only)
	uv run kun idle-batch --only health_report

preflight:  ## run production-style static/runtime preflight
	uv run kun ops preflight

readiness:  ## run V4 readiness summary (preflight + secrets + delivery)
	uv run kun ops readiness

dogfood:  ## run low-risk V4 dogfood smoke
	uv run kun ops dogfood --tenant $${KUN_DEFAULT_TENANT_ID:-u-sylvan}

delivery-status:  ## show honest ready/partial/not_ready status
	uv run kun ops delivery-status

clean:  ## remove caches
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name "__pycache__" -exec rm -rf {} +
