# mini-lean-agent

A thin Lean specialization of [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent).
Upstream supplies the agent loop, model providers, bash tool, completion command, and trajectories.
This package adds a Lean prompt, a sequential batch adapter, and verification through
[Lean FRO's comparator](https://github.com/leanprover/comparator).

## Install

```bash
git clone https://github.com/TomasOrtega/mini-lean-agent.git
cd mini-lean-agent
python -m pip install -e .
```

Install `comparator` following its [upstream setup and sandbox requirements](https://github.com/leanprover/comparator#readme).
It needs Lean, Linux with working `landrun`, and `lean4export` compatible with the project's Lean
version. Put the binaries on `PATH`, or select the comparator with `--comparator /path/to/comparator`.
The examples and comparator integration tests use Lean and comparator tag `v4.33.0`.

## Prepare verification

Use a separate, trusted Lake project containing:

- `Challenge.lean`: the original theorem, with the required declaration name and trusted imports.
- `lakefile.toml` or `lakefile.lean`: libraries named `Challenge` and `Solution`.
- A pinned `lean-toolchain` and prepared, trusted dependencies.

[examples/verification](examples/verification) provides a minimal project for the core example.
`Solution.lean` is reserved: each verification overwrites it with the agent's target file. Other
agent files and build configuration are not copied. Give each task its own verification project.

Keep the agent in an isolated workspace and protect the verification project, dependencies, and
toolchain with operating-system permissions. Separate paths alone provide no sandbox. See
[SECURITY.md](SECURITY.md).

## Solve

Configure a model supported by mini-SWE-agent, then run:

```bash
export MSWEA_MODEL_NAME='openrouter/<provider>/<model>'
export OPENROUTER_API_KEY='...'
mini-lean solve "Replace the sorry in MiniLeanExample.lean." \
  --project examples/core \
  --target MiniLeanExample.lean \
  --verification-project examples/verification \
  --declaration and_comm_example
```

The model edits the target and checks its proof through bash. It finishes with the standalone
command `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`. The runner then verifies the target; a rejected
submission reports `verification_failed`. There are no further proof attempts after submitting.

## Verify

```bash
mini-lean check MiniLeanExample.lean \
  --project examples/core \
  --verification-project examples/verification \
  --declaration and_comm_example
```

Both `solve` and `check` require the verification project and declaration. The runner writes a
transient specification and runs `lake env comparator` there. Comparator checks the theorem and its
statement dependencies against `Challenge`, checks permitted axioms, and replays the proof in Lean's
kernel. The allowed axioms are `propext`, `Quot.sound`, and `Classical.choice`. Missing tools, timeouts,
and comparator failures reject the result.

## Batch and trajectories

Each JSONL row requires `task`, `project`, `target`, `verification_project`, and `declaration`;
`id` is optional. `project` and `verification_project` resolve relative to the JSONL file.

```json
{"id":"core-and-comm","task":"Replace the sorry.","project":"core","target":"MiniLeanExample.lean","verification_project":"verification","declaration":"and_comm_example"}
```

```bash
mini-lean batch examples/tasks.jsonl --output-dir results
mini-extra inspect results/core-and-comm.traj.json
```

The batch adapter writes upstream trajectories and `summary.jsonl`. Individual solves default to
`<project>/.mini-lean-agent/trajectories/`. Verification metadata lives under `info.mini_lean_agent`.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=minileanagent
uv build
```

Set `MINI_LEAN_TEST_COMPARATOR` to a built comparator to run the real proof-verification tests.

## License and attribution

MIT; see [LICENSE](LICENSE). mini-SWE-agent's attribution and license are preserved in
[NOTICE.md](NOTICE.md) and [third_party/mini-swe-agent-LICENSE.md](third_party/mini-swe-agent-LICENSE.md).
Comparator is an external tool maintained by Lean FRO; follow its upstream license and setup.
