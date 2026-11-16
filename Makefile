.PHONY: setup lint format test test-metadata test-neo4j demo clean

UV := $(shell command -v uv 2>/dev/null)

setup:  ## create .venv with every dependency (uv fetches Python 3.12+ itself if needed)
ifdef UV
	uv sync --dev --all-extras
else
	@echo "make setup needs uv. Install it with one command, then rerun make setup:"
	@echo "  curl -LsSf https://astral.sh/uv/install.sh | sh     (macOS / Linux)"
	@echo "  brew install uv                                      (Homebrew)"
	@echo "See https://docs.astral.sh/uv/getting-started/installation/"
	@exit 1
endif

lint:
	uv run ruff check . && uv run ruff format --check .

format:
	uv run ruff check --fix . && uv run ruff format .

test: lint  ## lint, the project's tests, and the metadata skill's tests
	uv run pytest -q
	$(MAKE) --no-print-directory test-metadata

test-metadata:  ## the croissant-semantics skill's tests, with that skill's own dependencies
	cd .agents/skills/metadata/croissant-semantics && uv run --with pytest --with requests \
		--with jsonpath-ng --with pyyaml --with bioregistry --with openpyxl --with truststore \
		--with pypdf --with pyarrow --with xlrd pytest -q tests/

test-neo4j:  ## the MCP server against a toy graph in a throwaway Neo4j container (needs Docker)
	bash tests/neo4j_smoke.sh

demo:  ## dry run every stage on toy data; must always work on main
	uv run python -m shared.pipeline --dry-run

clean:
	rm -rf .venv .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
