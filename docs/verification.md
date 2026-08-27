# Verification

The verifier resolves one `.lean` target inside the selected project and runs:

- `lake env lean TARGET` when a Lake project is present
- `lean TARGET` otherwise
- a custom tokenized command when `--lean-command` is supplied

A custom command may contain `{target}`. The verifier replaces that token with the project-relative
file name. It never invokes an additional shell.

The result is rejected when:

- Lean returns a nonzero exit code
- verification times out
- the executable is unavailable
- the target contains `sorry`, `admit`, or `sorryAx` outside comments and strings
- Lean emits the standard warning that a declaration uses `sorry`
- a protected declaration's elaborated type changed

Lean block comments can nest, so placeholder scanning uses a small lexer instead of a regular
expression over raw source.

## Declaration guard

For benchmark tasks, pass a fully-qualified name:

```bash
mini-lean solve "Replace the sorry." \
  --project . \
  --target Main.lean \
  --declaration MyProject.main_theorem
```

Before the model starts, the harness appends a temporary `#check` command and records the elaborated
type. It repeats the check after the run and compares normalized output. The temporary file is
removed immediately after each check.

## Boundary of the check

Compilation, placeholder rejection, and one declaration guard do not guarantee that imports, other
declarations, unrelated files, or axioms were unchanged. A benchmark can inspect the final diff and
run additional task-specific evaluators while leaving the agent baseline unchanged.
