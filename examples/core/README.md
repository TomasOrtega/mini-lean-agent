# Core Lean example

This tiny project uses only Lean core and contains one intentional `sorry`.

```bash
mini-lean doctor --project examples/core
mini-lean solve \
  "$(cat examples/core/task.txt)" \
  --project examples/core \
  --target MiniLeanExample.lean \
  --model "$MINI_LEAN_MODEL"
```

Restore the example after a run with Git:

```bash
git checkout -- examples/core/MiniLeanExample.lean
```
