# Repository guide for coding agents

mini-lean-agent is a thin downstream specialization of mini-SWE-agent. Do not introduce a second
agent loop, model adapter, shell implementation, trajectory schema, or tool-calling protocol.

## Ownership boundary

Upstream mini-SWE-agent owns:

- the agent loop and limits
- model-provider handling
- the bash tool
- subprocess execution
- trajectory serialization and inspection

This repository owns:

- `src/minileanagent/config/lean.yaml`: the Lean prompt
- `src/minileanagent/verify.py`: fixed-target Lean evaluation
- `src/minileanagent/submit.py`: verified completion through the upstream sentinel
- `src/minileanagent/runner.py`: direct construction of upstream classes
- `src/minileanagent/batch.py`: a small sequential JSONL adapter
- `src/minileanagent/cli.py`: Lean-facing commands

Native Lean tools, Kimina, retrieval, tactic environments, and proof search must remain explicit
ablations rather than changes to the default baseline.

## Required checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

Never commit credentials, generated trajectories, `.lake`, or private proof corpora.
