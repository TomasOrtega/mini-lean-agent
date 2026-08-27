# Changelog

## 0.2.0

- Replace the independent agent, model, shell, and trajectory implementations with the upstream
  mini-SWE-agent package.
- Keep exactly one model-visible tool, mini-SWE-agent's bash tool.
- Add a Lean-specific prompt and a fixed-target `mini-lean-submit` command.
- Independently verify the final artifact after every upstream run, including direct-sentinel exits.
- Add optional declaration-type preservation for benchmark-quality tasks.
- Add a small sequential JSONL adapter while retaining upstream trajectory serialization.
- Remove the native `lean_check` tool and other scaffold-specific abstractions from the baseline.

## 0.1.0

- Initial independent prototype.
