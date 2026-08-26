# Trajectories

Each run is saved as JSON after initialization and after every model step. Writes are atomic through
a temporary file and rename, so interrupted runs usually retain their latest complete state.

## Top-level fields

- `trajectory_schema`: compatibility identifier
- `run`: task, status, model, cost, steps, timestamps, and submission summary
- `config`: agent limits and output path
- `environment`: project, target, toolset, backend, and checker command
- `messages`: the linear model-visible history
- `events`: normalized model and tool metadata for analysis

## Linear history

The `messages` list follows the native tool-calling format:

```text
system
user
assistant with tool_calls
one tool message per call
assistant with tool_calls
...
```

No separate hidden memory is used by the default agent.

## Privacy

Trajectories may contain:

- private source code
- theorem statements and proofs
- shell output
- paths and usernames
- environment data printed by commands
- secrets accidentally exposed by tools

Review and redact trajectories before publishing or using them as training data.
