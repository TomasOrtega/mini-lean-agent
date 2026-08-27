# mini-lean-agent

mini-lean-agent is a minimal Lean baseline built directly on mini-SWE-agent.

It reuses upstream:

- `DefaultAgent`
- model-provider support
- the bash tool
- `LocalEnvironment`
- limits and error handling
- trajectory serialization and inspection

It adds:

- a Lean-specific prompt
- fixed-target verification
- `mini-lean-submit`
- optional theorem-statement preservation
- a small JSONL adapter

Start with the [quick start](quickstart.md), then read the [design](design.md) and
[verification boundary](verification.md).
