# Quick start

## Install

```bash
git clone https://github.com/TomasOrtega/mini-lean-agent.git
cd mini-lean-agent
python -m pip install -e .
```

## Configure a model

```bash
export MSWEA_MODEL_NAME='openrouter/<provider>/<model>'
export OPENROUTER_API_KEY='...'
```

You can also pass `--model` on each run.

## Solve a target

```bash
mini-lean solve \
  "Replace the sorry in MiniLeanExample.lean." \
  --project examples/core \
  --target MiniLeanExample.lean \
  --declaration and_comm_example
```

The model has mini-SWE-agent's bash tool and finishes by running:

```bash
mini-lean-submit
```

## Check a target directly

```bash
mini-lean check MiniLeanExample.lean --project examples/core
```

Override the checker for unusual projects:

```bash
mini-lean check Main.lean \
  --project . \
  --lean-command 'python scripts/check.py {target}'
```

## Inspect the upstream trajectory

```bash
mini-extra inspect .mini-lean-agent/trajectories/<run>.traj.json
```
