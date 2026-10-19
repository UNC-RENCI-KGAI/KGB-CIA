.PHONY: setup lint format test demo clean

UV := $(shell command -v uv 2>/dev/null)

setup:  ## create .venv and install in editable mode
ifdef UV
	uv sync --dev
else
	python3 -m venv .venv && .venv/bin/pip install -e . pytest ruff
endif

lint:
	uv run ruff check . && uv run ruff format --check .

format:
	uv run ruff check --fix . && uv run ruff format .

test: lint
	uv run pytest -q

demo:  ## dry run every stage on toy data; must always work on main
	uv run python -m shared.pipeline --dry-run

clean:
	rm -rf .venv .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
