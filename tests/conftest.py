from __future__ import annotations

import os
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
        r"""
from pathlib import Path
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

print(f"checked {path}")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (project / "Main.lean").write_text(
        "theorem Demo.complete : True := by trivial\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(checker), "{target}"])
    return project, command


@pytest.fixture
def fake_comparator(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    verification_project = tmp_path / "verifier"
    verification_project.mkdir()
    (verification_project / "lakefile.toml").write_text(
        'name = "verifier"\n[[lean_lib]]\nname = "Challenge"\n[[lean_lib]]\nname = "Solution"\n',
        encoding="utf-8",
    )
    (verification_project / "Challenge.lean").write_text(
        "theorem Demo.complete : True := by sorry\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lake = bin_dir / "lake"
    lake.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        'assert sys.argv[1] == "env"\nos.execv(sys.argv[2], sys.argv[2:])\n',
        encoding="utf-8",
    )
    lake.chmod(0o755)
    comparator = bin_dir / "comparator"
    comparator.write_text(
        f"#!{sys.executable}\n"
        """
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
print(json.dumps(config), flush=True)
source = Path("Solution.lean").read_text()
if "BROKEN_CHECKER" in source:
    print("synthetic build failure", file=sys.stderr)
    raise SystemExit(1)
if any(word in source for word in ("sorry", "admit", "sorryAx")):
    print("Illegal axiom detected: sorryAx", file=sys.stderr)
    raise SystemExit(1)
if ("TYPE_FALSE" in source) != ("TYPE_FALSE" in Path("Challenge.lean").read_text()):
    print("Challenge and solution theorem statement do not match", file=sys.stderr)
    raise SystemExit(1)
print("Your solution is okay!")
""",
        encoding="utf-8",
    )
    comparator.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return verification_project, str(comparator)
