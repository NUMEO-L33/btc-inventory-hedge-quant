.PHONY: install test lint typecheck format check backtest research shadow status verify-data clean

PY ?= python

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	ruff check src tests

format:
	ruff format src tests && ruff check --fix src tests

typecheck:
	mypy

check: lint typecheck test

verify-data:
	$(PY) -m btc_inventory_quant data verify --raw-dir data/raw

backtest:
	$(PY) -m btc_inventory_quant backtest --config configs/backtest.yaml

research:
	$(PY) -m btc_inventory_quant research --config configs/backtest.yaml walk-forward

shadow:
	$(PY) -m btc_inventory_quant shadow --config configs/shadow.yaml

status:
	$(PY) -m btc_inventory_quant status --config configs/shadow.yaml

migrate:
	alembic upgrade head

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
