# Batch evaluation

A JSONL task has three required fields. `id` and `declaration` are optional, although declaration is
recommended for benchmarks:

```json
{"id":"task-1","task":"Replace the sorry.","project":"/tmp/task-1","target":"Main.lean","declaration":"Example.main"}
```

Relative project paths are resolved from the JSONL file's directory. Each project should be an
isolated checkout.

Run:

```bash
mini-lean batch tasks.jsonl \
  --model "$MSWEA_MODEL_NAME" \
  --output-dir results
```

The adapter is sequential on purpose. It creates one upstream mini-SWE-agent trajectory per task and
appends a row to `results/summary.jsonl`.

For a comparable result, report:

- mini-lean-agent and mini-SWE-agent versions
- repository commit and Lean toolchain
- model identifier and provider
- step, cost, wall-time, shell-timeout, and verification-timeout limits
- whether a protected declaration was supplied
- any evaluator applied beyond the built-in target check

Large-scale parallel scheduling should wrap this adapter rather than alter the agent loop.
