# Contributing

Contributions are welcome. The central design constraint is that this package remains a thin Lean
specialization of mini-SWE-agent.

## What belongs here

Changes to Lean prompts, target verification, benchmark adapters, and Lean-specific metadata belong
in this repository. Changes to generic agent control flow, provider support, sandboxing, trajectory
inspection, or bash execution should normally be contributed upstream to mini-SWE-agent.

New Lean capabilities such as native proof-state tools, Kimina, retrieval, or search should be
optional and evaluated against the unchanged bash-only baseline.

## Development setup

```bash
git clone https://github.com/TomasOrtega/mini-lean-agent.git
cd mini-lean-agent
uv sync --extra dev
uv run pre-commit install
```

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

Unit tests use a fake Lean executable. The `lean-smoke` CI job separately checks the verifier with a
real Lean toolchain. Tests that call a model provider must remain opt-in and must never require a
credential in pull-request CI.

## Pull requests

Explain the baseline behavior being changed, whether the upstream mini-SWE-agent compatibility
surface changes, and which tests cover the change. Update the changelog for user-visible behavior.
