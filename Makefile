PORT ?= 8000

.PHONY: help install up down db-init migrate seed backfill api worker decision positions dev status test lint fmt clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## create venv and install deps
	uv sync --extra dev
	@test -f .env || cp .env.example .env

up: ## ensure local postgres + redis are running and the db exists
	@pg_isready -q || brew services start postgresql@14
	@redis-cli ping >/dev/null 2>&1 || brew services start redis
	@until pg_isready -q; do sleep 1; done
	@until redis-cli ping >/dev/null 2>&1; do sleep 1; done
	@psql -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='asm'" | grep -q 1 || \
		psql -d postgres -c "CREATE ROLE asm LOGIN PASSWORD 'asm' SUPERUSER;"
	@psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='asm'" | grep -q 1 || \
		psql -d postgres -c "CREATE DATABASE asm OWNER asm;"
	@echo "postgres + redis ready"

down: ## stop local services
	-brew services stop redis
	-brew services stop postgresql@14

migrate: ## apply database migrations
	uv run alembic upgrade head

revision: ## autogenerate a migration (make revision m="add x")
	uv run alembic revision --autogenerate -m "$(m)"

db-init: up migrate ## bring up infra and create the schema

seed: ## import seeds/wallets.txt and queue scoring
	uv run python -m asm.cli seed

backfill: ## reconstruct history + score every watched wallet
	uv run python -m asm.cli backfill

status: ## portfolio + system state
	uv run python -m asm.cli status

run: ## START EVERYTHING (redis, postgres, db, all 4 services)
	@PORT=$(PORT) ./start.sh

stop: ## stop all services
	@./stop.sh

logs: ## tail all service logs
	@tail -f logs/*.log

ps: ## show which services are running
	@ps aux | grep -E "uvicorn asm|asm\.services" | grep -v grep || echo "nothing running"

api: ## run ONLY the API + dashboard (override with: make api PORT=8099)
	@lsof -nP -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1 && \
		{ echo "port $(PORT) is already in use - try: make api PORT=8099"; exit 1; } || true
	@echo "dashboard → http://localhost:$(PORT)   docs → http://localhost:$(PORT)/docs"
	uv run uvicorn asm.services.api.main:app --reload --port $(PORT)

worker: ## run the ARQ cold-path worker
	uv run arq asm.services.worker.WorkerSettings

decision: ## run the hot path (detection -> decision -> execution)
	uv run python -m asm.services.decision_service

positions: ## run the exit/position manager
	uv run python -m asm.services.position_service

backtest: ## replay history and print the verdict (gates live trading)
	uv run python -m asm.cli backtest

research: ## gate effectiveness, latency profile, trader contribution
	uv run python -m asm.cli research

graph: ## rebuild the wallet relationship graph
	uv run python -m asm.cli graph

harvest: ## evaluate and run profit harvesting now
	uv run python -m asm.cli harvest

dashboard: ## open the control center in a browser
	@open http://localhost:$(PORT) 2>/dev/null || echo "http://localhost:$(PORT)"

kill: ## EMERGENCY: stop signing immediately
	uv run python -m asm.cli kill

test: ## run tests
	uv run pytest -q

lint: ## lint
	uv run ruff check src tests

fmt: ## format
	uv run ruff format src tests && uv run ruff check --fix src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
