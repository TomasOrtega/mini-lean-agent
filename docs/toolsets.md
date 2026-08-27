# Tool interface

The default baseline exposes exactly one model-visible tool:

- `bash(command)` from mini-SWE-agent

The model interacts with Lean through ordinary commands:

```bash
lake env lean Main.lean
rg "theorem.*compact" .lake/packages/mathlib/Mathlib
cat > Scratch.lean <<'LEAN'
import Mathlib
#check Set.Finite
LEAN
lake env lean Scratch.lean
```

The scaffold does not decide what counts as theorem search, inspection, editing, or checking. This
is the central comparability claim: the control flow and tool interface are mini-SWE-agent's, with a
Lean prompt and evaluator around them.

To request completion, the model runs:

```bash
mini-lean-submit
```

This is an ordinary shell command, not an additional model tool. It reads the fixed target from the
harness environment and accepts no target argument.

Native `lean_check`, proof-state actions, Kimina, retrieval, and tactic search can be evaluated as
future ablations. They are not part of the default package.
