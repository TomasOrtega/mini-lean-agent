from __future__ import annotations

from pathlib import Path

import pytest

from minileanagent.environment import LocalLeanEnvironment
from minileanagent.types import ToolCall


def call(environment: LocalLeanEnvironment, name: str, **arguments: object) -> dict[str, object]:
    return environment.execute(ToolCall(id="test", name=name, arguments=dict(arguments)))


def test_bash_calls_are_fresh_but_files_persist(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    environment = LocalLeanEnvironment(project, lean_command=command)

    first = call(
        environment,
        "bash",
        command="export MINI_LEAN_TEST=present; echo data > state.txt",
    )
    second = call(
        environment,
        "bash",
        command='printf "%s:%s" "${MINI_LEAN_TEST-unset}" "$(cat state.txt)"',
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["output"] == "unset:data"


def test_lean_check_path_code_and_submission(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    target = project / "Main.lean"
    target.write_text("theorem good : True := by trivial\n", encoding="utf-8")
    environment = LocalLeanEnvironment(
        project, target="Main.lean", lean_command=command, toolset="lean"
    )

    checked = call(environment, "lean_check", path="Main.lean")
    snippet = call(environment, "lean_check", code="theorem alsoGood : True := by trivial")
    submitted = call(environment, "bash", command="mini-lean-submit")

    assert checked["ok"] is True
    assert snippet["ok"] is True
    assert submitted["ok"] is True
    assert submitted["terminal"] is True
    assert submitted["summary"] == "Verified Main.lean"
    scratch = project / ".mini-lean-agent" / "scratch"
    assert list(scratch.glob("*.lean")) == []


def test_sorry_and_errors_are_rejected(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    target = project / "Main.lean"
    target.write_text("theorem bad : True := by sorry\n", encoding="utf-8")
    environment = LocalLeanEnvironment(project, target=target, lean_command=command)

    sorry_result = call(environment, "bash", command="mini-lean-submit")
    target.write_text("BROKEN\n", encoding="utf-8")
    broken_result = call(environment, "bash", command="mini-lean-submit")

    assert sorry_result["ok"] is False
    assert sorry_result["contains_sorry"] is True
    assert sorry_result["terminal"] is False
    assert broken_result["ok"] is False
    assert broken_result["returncode"] == 1


def test_tool_validation_and_path_containment(
    lean_project: tuple[Path, str], tmp_path: Path
) -> None:
    project, command = lean_project
    environment = LocalLeanEnvironment(project, lean_command=command)

    assert call(environment, "lean_check")["ok"] is False
    assert call(environment, "lean_check", code="#check Nat", path="Main.lean")["ok"] is False
    assert call(environment, "unknown")["ok"] is False
    assert call(environment, "bash", command="")["ok"] is False
    with pytest.raises(ValueError, match="escapes"):
        LocalLeanEnvironment(project, target=tmp_path.parent / "outside.lean", lean_command=command)


def test_output_is_truncated(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    environment = LocalLeanEnvironment(project, lean_command=command, max_output_chars=100)
    result = call(environment, "bash", command="python -c 'print(\"x\" * 1000)'")

    assert result["ok"] is True
    assert result["elided_chars"] > 0
    assert "characters elided" in str(result["output"])


def test_bash_timeout(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    environment = LocalLeanEnvironment(project, lean_command=command, command_timeout_seconds=1)
    result = call(environment, "bash", command="sleep 2")

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["returncode"] == 124


def test_missing_checker_and_submission_errors(lean_project: tuple[Path, str]) -> None:
    project, _ = lean_project
    environment = LocalLeanEnvironment(
        project, lean_command="definitely-not-a-command", toolset="lean"
    )
    missing_command = call(environment, "lean_check", code="#check Nat")
    no_target = call(environment, "bash", command="mini-lean-submit")
    missing_file = call(environment, "bash", command="mini-lean-submit Nope.lean")

    assert missing_command["returncode"] == 127
    assert missing_command["ok"] is False
    assert "No path" in str(no_target["error"])
    assert "does not exist" in str(missing_file["error"])


def test_kimina_backend_with_mock_client(
    lean_project: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import SimpleNamespace

    project, _ = lean_project

    class Response:
        def __init__(self, severity: str = "info", data: str = "ok") -> None:
            self.severity = severity
            self.data = data

        def model_dump(self, mode: str = "json") -> dict[str, object]:
            return {
                "results": [
                    {
                        "response": {
                            "messages": [{"severity": self.severity, "data": self.data}]
                        },
                        "error": None,
                    }
                ]
            }

    class Client:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def check(self, code: str, **kwargs: object) -> Response:
            if "BAD" in code:
                return Response("error", "bad")
            if "SORRY" in code:
                return Response("warning", "declaration uses 'sorry'")
            return Response()

    monkeypatch.setitem(sys.modules, "kimina_client", SimpleNamespace(KiminaClient=Client))
    environment = LocalLeanEnvironment(project, checker_backend="kimina", toolset="lean")

    assert call(environment, "lean_check", code="#check Nat")["ok"] is True
    assert call(environment, "lean_check", code="BAD")["ok"] is False
    sorry = call(environment, "lean_check", code="SORRY")
    assert sorry["ok"] is False
    assert sorry["contains_sorry"] is True


def test_invalid_toolset_and_backend(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    with pytest.raises(ValueError, match="toolset"):
        LocalLeanEnvironment(project, toolset="many", lean_command=command)
    with pytest.raises(ValueError, match="checker_backend"):
        LocalLeanEnvironment(project, checker_backend="other", lean_command=command)


def test_submission_cannot_bypass_configured_target(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    (project / "Main.lean").write_text("BROKEN\n", encoding="utf-8")
    (project / "Trivial.lean").write_text("theorem easy : True := by trivial\n", encoding="utf-8")
    environment = LocalLeanEnvironment(project, target="Main.lean", lean_command=command)

    result = call(environment, "bash", command="mini-lean-submit Trivial.lean")

    assert result["ok"] is False
    assert "configured target" in str(result["error"])


def test_submit_protocol_rejects_compound_commands(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    environment = LocalLeanEnvironment(project, target="Main.lean", lean_command=command)

    result = call(environment, "bash", command="mini-lean-submit Main.lean && echo bypass")

    assert result["ok"] is False
    assert "Do not combine" in str(result["error"])


def test_runtime_validation_and_limits(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    environment = LocalLeanEnvironment(project, lean_command=command, toolset="lean")

    assert call(environment, "lean_check", path=123)["ok"] is False
    assert call(environment, "lean_check", code="")["ok"] is False
    with pytest.raises(ValueError, match="command_timeout"):
        LocalLeanEnvironment(project, lean_command=command, command_timeout_seconds=0)
    with pytest.raises(ValueError, match="max_output"):
        LocalLeanEnvironment(project, lean_command=command, max_output_chars=0)


def test_toolsets_expose_exact_schemas(lean_project: tuple[Path, str]) -> None:
    project, command = lean_project
    bash_environment = LocalLeanEnvironment(
        project, lean_command=command, toolset="bash"
    )
    lean_environment = LocalLeanEnvironment(
        project, lean_command=command, toolset="lean"
    )

    bash_names = [tool["function"]["name"] for tool in bash_environment.tool_schemas()]
    lean_names = [tool["function"]["name"] for tool in lean_environment.tool_schemas()]

    assert bash_names == ["bash"]
    assert lean_names == ["bash", "lean_check"]
