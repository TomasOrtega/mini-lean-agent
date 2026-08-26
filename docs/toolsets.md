# Toolsets

mini-lean-agent exposes two controlled toolsets.

## `bash`

Model-visible tools:

- `bash(command)`

The model interacts with Lean through ordinary commands such as:

```bash
lake env lean Main.lean
rg "theorem.*compact" .lake/packages/mathlib/Mathlib
cat > Scratch.lean <<'LEAN'
import Mathlib
#check Set.Finite
LEAN
lake env lean Scratch.lean
```

This is the closest analogue of mini-SWE-agent. Bash is a universal interface and the scaffold does
not decide what constitutes theorem search, inspection, editing, or checking.

To finish, the model makes a `bash` call containing only:

```bash
mini-lean-submit
```

This uses the configured target. Without one, it supplies a project-relative file:

```bash
mini-lean-submit Main.lean
```

The environment intercepts this reserved command, verifies the file, and terminates only on a clean
Lean check. It is part of the bash protocol, not an additional model-visible tool.

## `lean`

Model-visible tools:

- `bash(command)`
- `lean_check(path?, code?)`

`lean_check` accepts exactly one of:

- `path`: check a project-relative Lean file
- `code`: check a complete Lean snippet written to an ephemeral project-local file

The structured tool reduces boilerplate and makes verification events easier to identify in
trajectories. It does not expose proof states or choose tactics.

## Recommended ablation

Hold fixed:

- task checkout
- model and sampling settings
- prompts except tool descriptions
- checker backend
- step, cost, wall-time, timeout, and output limits
- final success criterion

Compare:

1. `--toolset bash`
2. `--toolset lean`

Report success rate, model calls, cost, wall time, verifier calls, command failures, and the fraction
of runs that reach a rejected submission before succeeding.
