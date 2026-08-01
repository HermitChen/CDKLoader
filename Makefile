UV_PROJECT_ENVIRONMENT ?= ../venv
export UV_PROJECT_ENVIRONMENT

.PHONY: sync test check frontend-install frontend-build dev serve docker-build docker-up docker-rebuild docker-down

sync:
	uv sync --locked --dev --python $(UV_PROJECT_ENVIRONMENT)/bin/python

test:
	uv run pytest

check:
	uv run python -m compileall -q app

frontend-install:
	npm --prefix frontend ci

frontend-build:
	npm --prefix frontend run build

dev:
	uv run uvicorn app.main:app --host 127.0.0.1 --port 1456 --reload

serve:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 1456

docker-build:
	bash build.sh

docker-up:
	docker compose up -d

docker-rebuild:
	bash build.sh
	docker compose up -d --force-recreate --remove-orphans

docker-down:
	docker compose down
