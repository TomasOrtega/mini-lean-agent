# Contributing

Keep the ownership boundary clear. Generic agent, provider, shell, sandbox, or trajectory changes
belong upstream in mini-SWE-agent. Lean prompts, verification, and benchmark adapters belong here.

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

The unit suite uses a fake Lean checker. GitHub Actions also runs a real Lean smoke test.

See the repository-level `CONTRIBUTING.md` and `RELEASING.md` for pull-request and release details.
