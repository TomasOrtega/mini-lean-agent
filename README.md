<div align="center">
  <img src="https://raw.githubusercontent.com/TomasOrtega/mini-lean-agent/main/docs/assets/banner.svg" alt="mini-lean-agent" width="760" />
</div>

# The minimal Lean agent baseline

[![CI](https://github.com/TomasOrtega/mini-lean-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/TomasOrtega/mini-lean-agent/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue)](https://tomasortega.github.io/mini-lean-agent/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/mini-lean-agent.svg)](https://pypi.org/project/mini-lean-agent/)

**mini-lean-agent** is a small, reproducible baseline for Lean 4 theorem-proving and
formalization tasks. It keeps the scaffold deliberately simple so that improvements can be
attributed to the model, training method, or tool interface rather than a large orchestration
system.

The design is inspired by [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent):

- one linear message history
- one fresh subprocess per shell action
- no hidden planner, memory database, proof search tree, or hand-written tactic policy
- complete JSON trajectories for debugging, fine-tuning, and RL
- an installable CLI and batch runner

The Lean-specific question is made into an explicit ablation:

| Toolset | Model-visible tools | Purpose |
|---|---|---|
| `bash` | `bash` | Strict mini-SWE-style baseline. Lean is used through shell commands. |
| `lean` | `bash`, `lean_check` | Adds one structured incremental-verification tool. |

The default is `bash`. Report both whenever the effect of specialized Lean tool use matters.

> [!WARNING]
> The model can execute arbitrary shell commands in the selected project. Use a disposable
> checkout, container, VM, or other sandbox for untrusted models and tasks.

## Quick start

Install from the repository:

```bash
git clone https://github.com/TomasOrtega/mini-lean-agent.git
cd mini-lean-agent
python -m pip install -e .
```

After the first PyPI release, the same CLI is available without a persistent installation:

```bash
uvx mini-lean-agent --help
```

Configure a tool-calling model supported by LiteLLM. For example, with OpenRouter:

```bash
export OPENROUTER_API_KEY=...
export MINI_LEAN_MODEL='openrouter/<provider>/<model>'
```

Run the agent in an existing Lean project:

```bash
mini-lean solve \
  "Replace the sorry in Main.lean without changing the theorem statement." \
  --project /path/to/project \
  --target Main.lean
```

Run the strict bash-only baseline:

```bash
mini-lean solve \
  "Prove the theorem in Main.lean." \
  --project /path/to/project \
  --target Main.lean \
  --toolset bash
```

Check the environment before an expensive run:

```bash
mini-lean doctor --project /path/to/project
mini-lean check Main.lean --project /path/to/project
```

Every run writes a trajectory under `.mini-lean-agent/trajectories/` unless `--trajectory`
is provided:

```bash
mini-lean inspect .mini-lean-agent/trajectories/<run>.json --full
```

## Control flow

```text
system prompt + Lean task
          │
          ▼
        model
          │ tool call
          ▼
 ┌─────────────────────┐
 │ bash                │  fresh subprocess, persistent files
 │ lean_check          │  optional structured verifier
 │ mini-lean-submit    │  reserved bash command, final verifier
 └─────────────────────┘
          │ observation
          └──────────────► append to the same linear history
```

The agent loop itself lives in [`src/minileanagent/agent.py`](src/minileanagent/agent.py).
The local and Kimina verifiers live in
[`src/minileanagent/environment.py`](src/minileanagent/environment.py).

## Local and Kimina verification

The reference backend runs the project-native command:

- `lake env lean FILE.lean` when a Lake project is detected
- `lean FILE.lean` otherwise
- a custom command supplied with `--lean-command` when needed

For high-throughput, self-contained Mathlib snippets, install the Kimina extra and point the
agent at a Kimina Lean Server:

```bash
python -m pip install 'mini-lean-agent[kimina]'
mini-lean solve \
  "Prove the theorem in Main.lean." \
  --project . \
  --target Main.lean \
  --checker kimina \
  --kimina-url http://localhost:8000
```

Kimina checks code in the server's configured Lean project. Use the local backend for repositories
with project-specific imports unless the Kimina server was configured for that project.

## Batch evaluation

Create `tasks.jsonl`:

```json
{"id":"example-1","task":"Replace the sorry in Main.lean.","project":"/tmp/task-1","target":"Main.lean"}
{"id":"example-2","task":"Prove theorem foo.","project":"/tmp/task-2","target":"Foo.lean"}
```

Run sequentially:

```bash
mini-lean batch tasks.jsonl \
  --model "$MINI_LEAN_MODEL" \
  --toolset bash \
  --output-dir results/bash

mini-lean batch tasks.jsonl \
  --model "$MINI_LEAN_MODEL" \
  --toolset lean \
  --output-dir results/lean
```

Each task should have an isolated checkout. This prevents one agent's edits from affecting another
and makes trajectories replayable.

## What to report

For a comparable baseline, record:

- repository commit and Lean toolchain
- model identifier and provider
- `bash` or `lean` toolset
- local or Kimina checker
- step, cost, wall-time, command-timeout, and output limits
- success criterion and whether theorem statements were frozen
- total model cost and number of model calls

The trajectory schema records these fields wherever the runner can observe them.

## Evaluation boundary

The built-in verifier fixes the submitted path and rejects compiler errors and `sorry` warnings. It
does not automatically prove that theorem statements, imports, axioms, or other source declarations
were left unchanged. Benchmark harnesses should inspect the final diff or wrap `--lean-command` with
a task-specific evaluator that enforces those invariants.

## Scope

mini-lean-agent is intentionally **not**:

- a tactic-level MDP such as Pantograph
- a retrieval system over Mathlib
- a proof-tree or best-first-search controller
- a multi-agent formalization pipeline
- a theorem-statement repair system

Those can be layered on top and compared against this baseline.

## Development

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [RELEASING.md](RELEASING.md), and the
[design documentation](https://tomasortega.github.io/mini-lean-agent/design/).

## Attribution

This project is an independent Lean-focused implementation inspired by the minimal-agent design of
mini-SWE-agent. It does not copy or vendor mini-SWE-agent. See
[NOTICE.md](NOTICE.md) for attribution and related projects.

## License

MIT. See [LICENSE](LICENSE).
