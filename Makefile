PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: venv install dev test lint fmt run openapi docker-up docker-down clean bench

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

dev: install

run:
	$(BIN)/uvicorn ratelimiter.service.asgi:app --reload --port 8000

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check src tests

fmt:
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

openapi:
	$(BIN)/python scripts/export_openapi.py

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down -v

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info src/*.egg-info
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +

bench:
	$(BIN)/python bench/bench.py
