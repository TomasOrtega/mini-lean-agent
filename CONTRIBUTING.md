# Contributing

Contributions are welcome, especially changes that preserve the project's value as a small,
controlled research baseline.

## Design constraint

The default agent should remain easy to audit. New planners, retrieval systems, proof-tree search,
memory, or tactic policies belong in optional modules or downstream projects rather than the core
loop. A useful rule is that a reader should be able to understand `agent.py` in one sitting.

## Development setup

```bash
git clone https://github.com/TomasOrtega/mini-lean-agent.git
cd mini-lean-agent
uv sync --all-extras
uv run pre-commit install
```

Run the full local checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

Tests must not require a network connection, an API key, Lean, or Kimina. Use the fake Lean checker
and model adapters in the test suite. Integration tests that require external services should be
opt-in and clearly marked.

## Pull requests

Keep pull requests focused. Explain:

1. the problem being solved
2. whether behavior or trajectory schemas change
3. which baseline comparison the change enables
4. tests added or updated

User-facing changes should update the changelog and documentation. Backward-incompatible trajectory
changes require a new `trajectory_schema` identifier.

## Commit style

Use short imperative subjects. Conventional Commit prefixes such as `feat:`, `fix:`, `docs:`, and
`test:` are encouraged but not required.
