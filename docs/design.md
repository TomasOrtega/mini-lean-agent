# Design

## Goal

mini-lean-agent is a baseline, not a claim that every Lean task should be solved by a minimal agent.
The goal is to make the scaffold small enough that experiments can identify where gains come from.

## Linear control flow

The complete loop is:

```text
initialize system and task messages
while within the limits:
    model_turn = model(messages, tool_schemas)
    append model_turn
    for tool_call in model_turn:
        observation = environment.execute(tool_call)
        append observation
        stop only after a verified `mini-lean-submit` command
```

There is no hidden state summarizer, plan graph, subagent manager, theorem queue, retrieval index, or
proof-tree search. The saved messages are the messages used by the model.

## Stateless command execution

Each `bash` call starts a new process with the project root as its working directory. File edits
persist. Shell state does not. This avoids long-lived shell corruption and makes it straightforward
to replace the local process runner with a container or cluster execution backend.

## Verified termination

A model cannot finish by merely claiming success. It must run `mini-lean-submit` through `bash`.
The environment compiles the submitted file through the configured checker and looks for Lean's warning that a declaration uses
`sorry`. A failed submission returns an ordinary observation and the model may continue.

## Model interface

The initial release uses native function/tool calling through LiteLLM. The model adapter is isolated
in `models.py`; alternate adapters can preserve the same agent and trajectory format.

A text-only protocol is a natural extension for models without reliable native tool calling. It
should be introduced as a distinct protocol setting rather than silently changing the baseline.

## Deliberate exclusions

The core does not include:

- Mathlib embedding retrieval
- LeanDojo-style premise selection
- tactic-state stepping
- best-first or Monte Carlo search
- proof decomposition
- theorem-statement mutation
- persistent memories
- multiple agents

These are valuable research directions. They should be evaluated as layers above the minimal runner.
