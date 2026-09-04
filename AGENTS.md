# Repository guide for coding agents

Keep this a thin specialization of mini-SWE-agent. Upstream owns the agent loop, model providers,
bash execution, completion sentinel, and trajectory format. Do not reimplement them.

This repository owns:

- `src/minileanagent/config/lean.yaml`: the Lean prompt.
- `src/minileanagent/runner.py`: construction of upstream classes and final evaluation.
- `src/minileanagent/verify.py`: staging the target and invoking Lean FRO's comparator.
- `src/minileanagent/batch.py`: sequential JSONL tasks.
- `src/minileanagent/cli.py`: Lean-facing commands.

Comparator owns statement comparison, axiom validation, and kernel checking. Do not add a Python
Lean parser or declaration fingerprint. Keep the trusted verification project separate from the
agent workspace; copy only the final target into its reserved `Solution.lean`. Do not compile
untrusted solutions in the verification project outside comparator.

Native Lean tools, retrieval, and proof search must remain explicit ablations.
Keep user documentation in README.md rather than adding another documentation site.

## Required checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

Never commit credentials, generated trajectories, `.lake`, or private proof corpora.
