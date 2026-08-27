# Core Lean example

This tiny project contains one intentional `sorry`.

```bash
export MSWEA_MODEL_NAME='openrouter/<provider>/<model>'
mini-lean doctor --project examples/core --target MiniLeanExample.lean
mini-lean solve \
  "$(cat examples/core/task.txt)" \
  --project examples/core \
  --target MiniLeanExample.lean \
  --declaration and_comm_example
```

Restore the example after a run:

```bash
git checkout -- examples/core/MiniLeanExample.lean
```
