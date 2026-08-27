from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest


def _install_minisweagent_unit_test_stub() -> None:
    """Provide only the upstream merge helper when dependencies are unavailable offline."""
    import importlib.util
    import types
    from typing import Any

    if importlib.util.find_spec("minisweagent") is not None:
        return

    package = types.ModuleType("minisweagent")
    package.__path__ = []  # type: ignore[attr-defined]
    utils = types.ModuleType("minisweagent.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    serialize = types.ModuleType("minisweagent.utils.serialize")
    unset = object()

    def recursive_merge(*dictionaries: dict[str, Any] | None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for dictionary in dictionaries:
            if dictionary is None:
                continue
            for key, value in dictionary.items():
                if value is unset:
                    continue
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = recursive_merge(result[key], value)
                elif isinstance(value, dict):
                    result[key] = recursive_merge(value)
                else:
                    result[key] = value
        return result

    serialize.UNSET = unset  # type: ignore[attr-defined]
    serialize.recursive_merge = recursive_merge  # type: ignore[attr-defined]
    sys.modules["minisweagent"] = package
    sys.modules["minisweagent.utils"] = utils
    sys.modules["minisweagent.utils.serialize"] = serialize


_install_minisweagent_unit_test_stub()


@pytest.fixture
def fake_lean_project(tmp_path: Path) -> tuple[Path, str]:
    """Create a project checked by a deterministic fake Lean executable."""
    project = tmp_path / "project"
    project.mkdir()
    checker = tmp_path / "fake_lean.py"
    checker.write_text(
        r'''
from pathlib import Path
import re
import sys
import time

path = Path(sys.argv[-1])
text = path.read_text(encoding="utf-8")
if "SLEEP_CHECKER" in text:
    time.sleep(5)
if "BROKEN_CHECKER" in text:
    print(f"{path}:1:1: error: synthetic Lean failure")
    raise SystemExit(1)
if "EMIT_SORRY_WARNING" in text:
    print(f"{path}:1:1: warning: declaration uses 'sorry'")

begin = re.search(r"MINI_LEAN_TYPE_BEGIN_[0-9a-f]+", text)
end = re.search(r"MINI_LEAN_TYPE_END_[0-9a-f]+", text)
declaration = re.search(r"#check @([A-Za-z0-9_'.]+)", text)
if begin and end and declaration:
    print(begin.group(0))
    result_type = "False" if "TYPE_FALSE" in text else "True"
    print(f"{declaration.group(1)} : {result_type}")
    print(end.group(0))
else:
    print(f"checked {path}")
'''.strip()
        + "\n",
        encoding="utf-8",
    )
    (project / "Main.lean").write_text(
        "theorem Demo.complete : True := by trivial\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(checker), "{target}"])
    return project, command
