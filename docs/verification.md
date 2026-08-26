# Verification backends

## Local backend

The local backend is the reference backend for repository tasks.

The checker selects:

- `lake env lean` when `lakefile.lean` or `lakefile.toml` exists
- `lean` otherwise

Override this with:

```bash
mini-lean solve "..." --lean-command 'lake env lean'
```

The submitted path must stay inside the configured project root. The checker captures combined
stdout and stderr, applies a per-call timeout, and truncates very long output by preserving its head
and tail.

## Kimina backend

Install the optional client:

```bash
python -m pip install 'mini-lean-agent[kimina]'
```

Start or connect to a Kimina Lean Server, then run:

```bash
mini-lean solve \
  "Prove the theorem in Main.lean." \
  --project . \
  --target Main.lean \
  --checker kimina \
  --kimina-url http://localhost:8000
```

Kimina evaluates code in the server's configured Lean project. It is particularly useful for
parallel checks of self-contained Mathlib snippets. Local repository imports will work only when the
server has been configured for the same project and dependencies.

## Soundness boundary

The runner verifies only what Lean verifies under the selected project and toolchain. It does not
attempt to impose a philosophical notion of allowed axioms beyond rejecting `sorry`/`admit` warnings.
Benchmarks that forbid additional axioms, unsafe declarations, theorem-statement changes, or native
code evaluation should add an external source audit and state those rules explicitly.
