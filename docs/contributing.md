# Contributing

The default agent is intended to remain small and auditable. Contributions that add sophisticated
search, retrieval, or orchestration should expose them as optional extensions or downstream systems
and retain the minimal baseline.

## Setup

```bash
uv sync --all-extras
uv run pre-commit install
```

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

Default tests are network-free and do not require Lean, Kimina, or model API keys.

See `CONTRIBUTING.md` in the repository for pull-request and trajectory-schema guidance.

Release maintainers should also follow `RELEASING.md`.
