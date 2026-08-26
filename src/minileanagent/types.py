"""Small shared data structures and protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

JsonDict = dict[str, Any]
Message = dict[str, Any]
ToolSchema = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One model-requested tool invocation."""

    id: str
    name: str
    arguments: JsonDict

    def as_openai(self) -> JsonDict:
        import json

        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(frozen=True)
class ModelTurn:
    """Normalized output from a language model."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    cost: float = 0.0
    usage: JsonDict = field(default_factory=dict)
    finish_reason: str | None = None

    def as_assistant_message(self) -> Message:
        message: Message = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [call.as_openai() for call in self.tool_calls]
        return message


class Model(Protocol):
    """The only model interface used by the agent loop."""

    @property
    def name(self) -> str: ...

    def query(self, messages: list[Message], tools: list[ToolSchema]) -> ModelTurn: ...


class Environment(Protocol):
    """The only environment interface used by the agent loop."""

    project: Path
    target: Path | None
    toolset: str

    def tool_schemas(self) -> list[ToolSchema]: ...

    def execute(self, call: ToolCall) -> JsonDict: ...

    def describe(self) -> JsonDict: ...
