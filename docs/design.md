# Design

## Reuse rather than reimplementation

The runner directly constructs mini-SWE-agent's public components:

- `minisweagent.agents.default.DefaultAgent`
- `minisweagent.environments.local.LocalEnvironment`
- the model returned by `minisweagent.models.get_model`

The classes are not copied or subclassed. Ordinary model calls, bash actions, subprocess execution,
limits, errors, and trajectory serialization therefore remain upstream behavior.

mini-lean-agent loads upstream `mini.yaml` and applies a small Lean prompt and environment overlay.
It then injects the fixed project and target into the subprocess environment, asks the model to
finish with `mini-lean-submit`, and independently evaluates the target after `DefaultAgent.run`
returns.

## Why one bash tool

Bash already lets a model edit files, run Lean, create scratch examples, inspect dependencies, and
write helper scripts. Keeping the interface unchanged makes comparisons with mini-SWE-agent clear
and keeps improvements attributable to the model rather than the scaffold.

## Submission and evaluation

`mini-lean-submit` reads the fixed target from environment variables supplied by the harness. It
cannot accept a different target argument. It verifies the file and prints mini-SWE-agent's standard
completion sentinel only when the check passes.

The submission command is a convenience boundary, not the final authority. After the upstream agent
exits, `run_task` evaluates the configured target again. A model that directly prints the sentinel
may end the upstream loop, but its mini-lean result is still a failure unless the target verifies.

When a fully-qualified declaration name is supplied, the runner records that declaration's
elaborated type before the run and compares it with the final type. This rejects changes to the
protected theorem statement without parsing Lean syntax in Python.

## Trajectories

The agent writes mini-SWE-agent's trajectory schema. The final save adds one namespaced object at
`info.mini_lean_agent` containing the target, versions, upstream exit status, and Lean verification
result.
