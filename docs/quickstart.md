# Quick start

## Requirements

- Python 3.10 or newer
- a Lean project with `lake` and/or `lean` available
- an API key for a tool-calling model supported by LiteLLM
- Bash on the execution host

## Install

From source:

```bash
git clone https://github.com/TomasOrtega/mini-lean-agent.git
cd mini-lean-agent
python -m pip install -e .
```

Once a PyPI release exists:

```bash
uvx mini-lean-agent --help
```

## Configure a model

Set a LiteLLM model identifier:

```bash
export MINI_LEAN_MODEL='openrouter/<provider>/<model>'
export OPENROUTER_API_KEY='...'
```

Provider-specific environment variables are passed through to LiteLLM. The optional
`MINI_LEAN_API_KEY` and `MINI_LEAN_API_BASE` variables work for custom OpenAI-compatible endpoints.

## Inspect the project

```bash
mini-lean doctor --project /path/to/project
mini-lean check Main.lean --project /path/to/project
```

`doctor` does not call a model. `check` uses the same verifier as final submission.

## Solve a task

```bash
mini-lean solve \
  "Replace the sorry in Main.lean without changing the theorem statement." \
  --project /path/to/project \
  --target Main.lean \
  --model "$MINI_LEAN_MODEL"
```

The default toolset is `bash`, which exposes only the universal shell tool. Add `--toolset lean` to expose `lean_check` as a controlled ablation. Finish either mode with a `bash` call whose complete command is `mini-lean-submit`.

The command above runs the strict bash-only baseline. Add the structured checker with:

```bash
mini-lean solve \
  "Replace the sorry in Main.lean." \
  --project /path/to/project \
  --target Main.lean \
  --toolset lean \
  --model "$MINI_LEAN_MODEL"
```

## Inspect a run

```bash
mini-lean inspect .mini-lean-agent/trajectories/<run>.json
mini-lean inspect .mini-lean-agent/trajectories/<run>.json --full
```

The full mode prints the exact linear message history sent to the model, including tool calls and
observations.
