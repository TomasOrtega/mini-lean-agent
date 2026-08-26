# Batch evaluation

## Task format

A batch file is JSON Lines. Each nonempty line contains:

```json
{
  "id": "unique-task-id",
  "task": "Natural-language instruction",
  "project": "/absolute/or/expanded/project/path",
  "target": "Main.lean"
}
```

`target` is optional, but a path must eventually be supplied with `mini-lean-submit PATH.lean`.

## Run

```bash
mini-lean batch tasks.jsonl \
  --model "$MINI_LEAN_MODEL" \
  --toolset bash \
  --output-dir results/bash
```

The initial batch runner is deliberately sequential. Parallel orchestration should create an
isolated checkout per task and run independent CLI processes. This keeps the single-run control flow
and trajectory semantics unchanged.

## Outputs

The output directory contains:

```text
results.jsonl
trajectories/
  task-1.json
  task-2.json
```

`results.jsonl` is suitable for aggregation. Trajectories retain the complete interaction for error
analysis.

## Fair comparisons

A benchmark harness should freeze or record:

- repository and dependency commits
- Lean version
- task statement and target file
- model endpoint and exact identifier
- temperature and output limit
- toolset and checker backend
- model-call, cost, wall-time, command-timeout, and output limits
- whether theorem statements and imports may change
- sandbox image and available shell programs

Do not run two agents concurrently in the same working tree.

## Source-level invariants

A clean Lean check alone does not establish that an agent preserved the requested theorem statement.
For benchmark-grade evaluation, inspect the final diff or use a task-specific wrapper supplied through
`--lean-command` to reject changes to protected declarations, imports, axioms, or other files. The
runner deliberately leaves these task-dependent rules outside the core agent.
