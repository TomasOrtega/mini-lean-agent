"""The complete agent control loop.

The loop is deliberately linear: query the model, append its tool call, execute the
call, append the observation, repeat. This makes saved trajectories directly usable
for debugging, supervised fine-tuning, and RL experiments.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from minileanagent.prompts import build_system_prompt, build_user_prompt
from minileanagent.types import Environment, JsonDict, Message, Model


@dataclass(frozen=True)
class AgentConfig:
    step_limit: int = 30
    cost_limit: float = 5.0
    wall_time_limit_seconds: int = 900
    max_consecutive_format_errors: int = 3
    trajectory_path: Path | None = None

    def __post_init__(self) -> None:
        if self.step_limit < 0:
            raise ValueError("step_limit must be nonnegative")
        if self.cost_limit < 0:
            raise ValueError("cost_limit must be nonnegative")
        if self.wall_time_limit_seconds < 0:
            raise ValueError("wall_time_limit_seconds must be nonnegative")
        if self.max_consecutive_format_errors <= 0:
            raise ValueError("max_consecutive_format_errors must be positive")


@dataclass(frozen=True)
class RunResult:
    status: str
    submission: str
    steps: int
    cost: float
    elapsed_seconds: float
    trajectory_path: Path | None


class MiniLeanAgent:
    """A minimal native-tool-calling Lean agent."""

    trajectory_schema = "mini-lean-agent-0.1"

    def __init__(self, model: Model, environment: Environment, config: AgentConfig | None = None):
        self.model = model
        self.environment = environment
        self.config = config or AgentConfig()
        self.messages: list[Message] = []
        self.events: list[JsonDict] = []
        self.cost = 0.0
        self.steps = 0
        self.status = "not_started"
        self.submission = ""
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None

    def run(self, task: str) -> RunResult:
        self.started_at = datetime.now(timezone.utc)
        self.finished_at = None
        started_monotonic = time.monotonic()
        self.status = "running"
        self.submission = ""
        self.cost = 0.0
        self.steps = 0
        self.events = []
        self.messages = [
            {
                "role": "system",
                "content": build_system_prompt(
                    project=self.environment.project,
                    target=self.environment.target,
                    toolset=self.environment.toolset,
                ),
            },
            {"role": "user", "content": build_user_prompt(task)},
        ]
        consecutive_format_errors = 0
        self._save(task)

        try:
            while self.status == "running":
                elapsed = time.monotonic() - started_monotonic
                limit_status = self._limit_status(elapsed)
                if limit_status:
                    self.status = limit_status
                    break

                turn = self.model.query(self.messages, self.environment.tool_schemas())
                self.steps += 1
                self.cost += turn.cost
                self.messages.append(turn.as_assistant_message())
                self.events.append(
                    {
                        "type": "model_turn",
                        "step": self.steps,
                        "cost": turn.cost,
                        "usage": turn.usage,
                        "finish_reason": turn.finish_reason,
                        "tool_names": [call.name for call in turn.tool_calls],
                    }
                )

                if not turn.tool_calls:
                    consecutive_format_errors += 1
                    available_tools = "`bash`"
                    if self.environment.toolset == "lean":
                        available_tools += " or `lean_check`"
                    self.messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"You did not call a tool. Call {available_tools} to continue. "
                                "After the target compiles, use `bash` with the stand-alone command "
                                "`mini-lean-submit`."
                            ),
                        }
                    )
                    if consecutive_format_errors >= self.config.max_consecutive_format_errors:
                        self.status = "format_error"
                    self._save(task)
                    continue

                consecutive_format_errors = 0
                for call in turn.tool_calls:
                    observation = self.environment.execute(call)
                    self.events.append(
                        {
                            "type": "tool_result",
                            "step": self.steps,
                            "tool_call_id": call.id,
                            "tool": call.name,
                            "arguments": call.arguments,
                            "observation": observation,
                        }
                    )
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(observation, ensure_ascii=False),
                        }
                    )
                    if observation.get("terminal") and observation.get("ok"):
                        self.status = "success"
                        self.submission = str(observation.get("summary") or "")
                        break
                self._save(task)
        except KeyboardInterrupt:
            self.status = "interrupted"
        except Exception as exc:
            self.status = "error"
            self.events.append(
                {
                    "type": "uncaught_exception",
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            self._save(task)
            raise
        finally:
            self.finished_at = datetime.now(timezone.utc)
            self._save(task)

        elapsed = time.monotonic() - started_monotonic
        return RunResult(
            status=self.status,
            submission=self.submission,
            steps=self.steps,
            cost=self.cost,
            elapsed_seconds=elapsed,
            trajectory_path=self.config.trajectory_path,
        )

    def _limit_status(self, elapsed_seconds: float) -> str | None:
        if self.config.step_limit > 0 and self.steps >= self.config.step_limit:
            return "step_limit"
        if self.config.cost_limit > 0 and self.cost >= self.config.cost_limit:
            return "cost_limit"
        if (
            self.config.wall_time_limit_seconds > 0
            and elapsed_seconds >= self.config.wall_time_limit_seconds
        ):
            return "time_limit"
        return None

    def serialize(self, task: str) -> JsonDict:
        return {
            "trajectory_schema": self.trajectory_schema,
            "run": {
                "task": task,
                "status": self.status,
                "submission": self.submission,
                "model": self.model.name,
                "steps": self.steps,
                "cost": self.cost,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            },
            "config": asdict(self.config),
            "environment": self.environment.describe(),
            "messages": self.messages,
            "events": self.events,
        }

    def _save(self, task: str) -> None:
        path = self.config.trajectory_path
        if path is None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.serialize(task), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)
