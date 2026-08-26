.PHONY: install lint format typecheck test build docs check

install:
	uv sync --all-extras

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy src

test:
	uv run pytest --cov=minileanagent

build:
	uv build

docs:
	uv run mkdocs serve

check: lint typecheck test build
