<div align="center">
  <img src="https://raw.githubusercontent.com/TomasOrtega/mini-lean-agent/main/docs/assets/banner.svg" alt="mini-lean-agent" width="760" />
</div>

# mini-lean-agent

[![CI](https://github.com/TomasOrtega/mini-lean-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/TomasOrtega/mini-lean-agent/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue)](https://tomasortega.github.io/mini-lean-agent/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**mini-lean-agent is mini-SWE-agent specialized for Lean.** It imports mini-SWE-agent's agent loop,
model support, bash tool, local subprocess environment, limits, and trajectory format. This
repository adds only a Lean prompt, fixed-target verification, a verified submission command, and a
small JSONL adapter.

The default baseline has exactly one model-visible tool:

```text
language model
     │
     │ bash
     ▼
mini-SWE-agent LocalEnvironment
     │
     ├── inspect and edit files
     ├── search the project and Mathlib
     ├── run Lean
     └── run mini-lean-submit
              │
              ▼
       fixed-target verifier
```

There is no second agent loop, model wrapper, shell implementation, trajectory schema, hidden
planner, tactic policy, retrieval service, or proof tree in this repository.

> [!WARNING]
> The model receives shell access through mini-SWE-agent. Run it in an isolated checkout, container,
> VM, or another sandbox. The Lean verifier is an evaluator, not a security sandbox.

## Installation

From source:

```bash
git clone https://github.com/TomasOrtega/mini-lean-agent.git
cd mini-lean-agent
python -m pip install -e .
```

The package pins mini-SWE-agent 2.4.6 exactly so a mini-lean-agent release has a reproducible
agent scaffold. The packaged Lean YAML is applied as an overlay on upstream `mini.yaml`, and every
trajectory records the installed and runtime upstream versions.

## Run one task

Set a model supported by mini-SWE-agent:

```bash
export MSWEA_MODEL_NAME='openrouter/<provider>/<model>'
export OPENROUTER_API_KEY='...'
```

Run the agent on a fixed target:

```bash
mini-lean solve \
  "Replace the sorry in MiniLeanExample.lean." \
  --project examples/core \
  --target MiniLeanExample.lean \
  --declaration and_comm_example
```

The optional `--declaration` argument is strongly recommended for benchmark runs. Before the model
starts, mini-lean-agent records the declaration's elaborated type. After the run, it verifies that
the type is unchanged.

The model uses ordinary shell commands such as:

```bash
rg "Finset.sum_congr" .lake/packages/mathlib/Mathlib
lake env lean MiniLeanExample.lean
cat > Scratch.lean <<'EOF'
import Mathlib
#check Finset.sum_congr
EOF
lake env lean Scratch.lean
```

When the proof is complete, the model runs:

```bash
mini-lean-submit
```

That command checks the fixed target and emits mini-SWE-agent's standard completion sentinel only on
success. The harness independently checks the artifact again after the upstream agent exits, so
directly printing the sentinel cannot produce a successful mini-lean result.

## Verify without running a model

```bash
mini-lean check MiniLeanExample.lean \
  --project examples/core
```

Lake projects use `lake env lean TARGET`. Other directories use `lean TARGET`. Unusual projects can
supply a tokenized command template:

```bash
mini-lean check Main.lean \
  --project . \
  --lean-command 'python scripts/check.py {target}'
```

The verifier rejects compiler failures, timeouts, missing executables, and `sorry`, `admit`, or
`sorryAx` outside comments and strings. It also rejects Lean's standard warning that a declaration
uses `sorry`.

## Trajectories

The output remains an ordinary mini-SWE-agent trajectory. By default it is stored at:

```text
<project>/.mini-lean-agent/trajectories/<timestamp>.traj.json
```

Inspect it with mini-SWE-agent's trajectory browser:

```bash
mini-extra inspect <trajectory-file>
```

Lean evaluation metadata is added under `info.mini_lean_agent`. No competing trajectory format is
introduced.

## Batch baseline

A JSONL row contains a task, isolated project, target, and preferably a protected declaration:

```json
{"id":"and-comm","task":"Replace the sorry.","project":"examples/core","target":"MiniLeanExample.lean","declaration":"and_comm_example"}
```

Run rows sequentially:

```bash
mini-lean batch examples/tasks.jsonl \
  --output-dir results/bash \
  --model "$MSWEA_MODEL_NAME"
```

The adapter writes one upstream trajectory per row and `summary.jsonl`. Parallel scheduling belongs
outside the baseline.

## Python API

```python
from minileanagent.runner import run_task

outcome = run_task(
    "Replace the sorry in Main.lean.",
    project="/path/to/project",
    target="Main.lean",
    declaration="MyProject.main_theorem",
    model_name="openrouter/<provider>/<model>",
)

assert outcome.verified
print(outcome.trajectory_path)
```

Internally, `run_task` directly constructs upstream `DefaultAgent`, `LocalEnvironment`, and the model
returned by mini-SWE-agent's `get_model` function.

## Evaluation boundary

The built-in evaluator fixes the target path, compiles that file, rejects proof placeholders, and can
preserve one declaration's elaborated type. It does not prove that imports, unrelated declarations,
other files, or the project's axiom set were unchanged. Stronger task-specific checks should wrap
this baseline rather than alter its agent loop.

Native Lean tools, Kimina, retrieval, tactic-level environments, and proof search are useful future
ablations. They are intentionally absent from the bash-only baseline.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

## Attribution

mini-lean-agent depends on [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent), released
under the MIT License by Kilian A. Lieret, Carlos E. Jimenez, and contributors. No mini-SWE-agent
source is vendored here. See [NOTICE.md](NOTICE.md) and
[third_party/mini-swe-agent-LICENSE.md](third_party/mini-swe-agent-LICENSE.md).

## License

MIT. See [LICENSE](LICENSE).
