# Repository guide for coding agents

mini-lean-agent is a deliberately small research baseline. Preserve the linear control flow and keep
specialized orchestration out of the default agent.

## Important files

- `src/minileanagent/agent.py`: model → tool → observation loop and trajectory format
- `src/minileanagent/environment.py`: shell, Lean checker, Kimina adapter, and submission verifier
- `src/minileanagent/models.py`: LiteLLM adapter
- `src/minileanagent/cli.py`: solve, check, doctor, inspect, and batch commands
- `tests/`: network-free unit tests using fake models and fake Lean checkers

## Required checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

Do not add tests that require API keys, network access, Lean, or Kimina to the default CI job. Do not
change theorem statements in examples merely to make proofs easier. Never commit generated
trajectories, secrets, `.lake`, or model-provider responses containing private data.
