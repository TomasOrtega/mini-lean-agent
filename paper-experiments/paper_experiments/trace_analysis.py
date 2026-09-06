"""Count requested shell programs and provider usage in the selected low-budget traces."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .protocol import GEMINI

PROGRAM_CATEGORIES = ("text_search", "cat", "sed", "python3", "lean_check", "file_discovery")
TOKEN_CATEGORIES = ("input", "output", "reasoning", "cache_read", "cache_write")


class EvidenceReader(Protocol):
    def bytes(self, path: Path, expected: str | None = None) -> bytes: ...

    def read(self, path: Path, expected: str | None = None) -> Any: ...


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _shell_tokens(command: str) -> list[tuple[str, bool]]:
    """Keep operators distinct from quoted text and omit literal heredoc bodies."""
    tokens: list[tuple[str, bool]] = []
    delimiters: list[tuple[str, bool]] = []
    pending_heredoc: bool | None = None
    i = 0
    while i < len(command):
        c = command[i]
        if c in " \t\r":
            i += 1
            continue
        if c == "#":
            i = command.find("\n", i)
            if i == -1:
                break
            continue
        if c in ";&|()<>\n":
            op = c
            if command[i : i + 3] == "<<-":
                op = "<<-"
            elif command[i : i + 2] in {"<<", ">>", "&&", "||", ";;", "<&", ">&"}:
                op = command[i : i + 2]
            tokens.append((op, True))
            i += len(op)
            if op in {"<<", "<<-"}:
                pending_heredoc = op == "<<-"
            if op == "\n":
                for delimiter, strip_tabs in delimiters:
                    while True:
                        end = command.find("\n", i)
                        line = command[i:] if end == -1 else command[i:end]
                        i = len(command) if end == -1 else end + 1
                        if (line.lstrip("\t") if strip_tabs else line).rstrip("\r") == delimiter:
                            break
                        _require(end != -1, "Unclosed heredoc")
                delimiters.clear()
            continue
        word = ""
        while i < len(command) and command[i] not in " \t\r;&|()<>\n":
            c = command[i]
            if c in "'\"":
                quote = c
                i += 1
                while i < len(command) and command[i] != quote:
                    if quote == '"' and command[i] == "\\":
                        i += 1
                        _require(i < len(command), "Unclosed escape")
                    word += command[i]
                    i += 1
                _require(i < len(command), "Unclosed quote")
                i += 1
            elif c == "\\":
                i += 1
                _require(i < len(command), "Unclosed escape")
                if command[i] != "\n":
                    word += command[i]
                i += 1
            else:
                word += c
                i += 1
        tokens.append((word, False))
        if pending_heredoc is not None:
            delimiters.append((word, pending_heredoc))
            pending_heredoc = None
    _require(not delimiters and pending_heredoc is None, "Unclosed heredoc")
    return tokens


def _program_categories(words: list[str]) -> set[str]:
    while words and (words[0] == "time" or re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", words[0])):
        words = words[1:]
    if not words:
        return set()
    program = Path(words[0]).name
    categories = set()
    if program in {"rg", "grep", "egrep", "fgrep"}:
        categories.add("text_search")
    if program in {"cat", "sed", "python3"}:
        categories.add(program)
    if program in {"find", "ls"}:
        categories.add("file_discovery")
    if program == "lean" or (
        program == "lake" and (words[1:3] == ["env", "lean"] or words[1:2] == ["build"])
    ):
        categories.add("lean_check")
    return categories


def classify_shell(command: str) -> tuple[set[str], bool]:
    """Count each named program family once per request, including conditional branches."""
    try:
        _require(bool(command.strip()), "Empty command")
        tokens = _shell_tokens(command)
    except ValueError:
        return _program_categories(command.split()[:1]), True
    categories: set[str] = set()
    words: list[str] = []
    redirect = False
    for token, operator in [*tokens, ("\n", True)]:
        if operator and token in {";", "&", "|", "&&", "||", ";;", "(", ")", "\n"}:
            categories.update(_program_categories(words))
            words = []
            redirect = False
        elif operator:
            redirect = True
        elif redirect:
            redirect = False
        else:
            words.append(token)
    return categories, False


def _shell_counts() -> dict[str, Any]:
    return {
        "bash_requests": 0,
        "program_requests": dict.fromkeys(PROGRAM_CATEGORIES, 0),
        "malformed_shell_requests": 0,
        "cat_and_lean_requests": 0,
    }


def _mini_requests(
    directory: Path, attempts: list[dict[str, Any]], evidence: EvidenceReader
) -> dict[str, Any]:
    response_ids = [a.get("response_metadata", {}).get("response_id") for a in attempts]
    response_ids = [value for value in response_ids if value]
    _require(len(response_ids) == len(set(response_ids)), "Duplicate ledger response ID")
    selected = set(response_ids)
    seen: dict[str, Any] = {}
    counts = _shell_counts()
    trajectory = evidence.read(directory / "trajectory.json")
    for message in trajectory["messages"]:
        if message.get("role") != "assistant":
            continue
        extra = message.get("extra") or {}
        response_id = (extra.get("response") or {}).get("id")
        if response_id not in selected:
            continue
        actions = extra.get("actions") or []
        if response_id in seen:
            _require(seen[response_id] == actions, "Conflicting duplicate response actions")
            continue
        seen[response_id] = actions
        action_ids: dict[str, Any] = {}
        for action in actions:
            action_id = action.get("tool_call_id")
            if action_id and action_id in action_ids:
                _require(action_ids[action_id] == action, "Conflicting duplicate action ID")
                continue
            if action_id:
                action_ids[action_id] = action
            counts["bash_requests"] += 1
            command = action.get("command")
            categories, malformed = (
                classify_shell(command) if isinstance(command, str) else (set(), True)
            )
            counts["malformed_shell_requests"] += malformed
            counts["cat_and_lean_requests"] += {"cat", "lean_check"} <= categories
            for category in categories:
                counts["program_requests"][category] += 1
    counts["responses_without_saved_message"] = len(selected - seen.keys())
    return counts


def _usage_group() -> dict[str, Any]:
    return {
        "calls": 0,
        "charge_usd": "0",
        "tokens": dict.fromkeys(TOKEN_CATEGORIES, 0),
        "missing_usage_calls": 0,
        "missing_charge_calls": 0,
    }


def _add_attempt(group: dict[str, Any], attempt: dict[str, Any]) -> None:
    group["calls"] += 1
    charge = attempt.get("charge_usd")
    group["missing_charge_calls"] += charge is None
    group["charge_usd"] = str(Decimal(group["charge_usd"]) + Decimal(charge or "0"))
    tokens = attempt.get("billable_tokens")
    group["missing_usage_calls"] += tokens is None
    for category, count in (tokens or {}).items():
        _require(
            category in TOKEN_CATEGORIES and type(count) is int and count >= 0,
            "Invalid token count",
        )
        group["tokens"][category] += count


def _accounting(
    row: dict[str, Any], endpoint: dict[str, Any], evidence: EvidenceReader
) -> dict[str, Any]:
    ledger = endpoint["ledger"]
    attempts = ledger["attempts"]
    _require(ledger["calls"] == len(attempts), "Low-endpoint call count differs")
    _require(len({a["attempt_id"] for a in attempts}) == len(attempts), "Duplicate ledger attempt")
    result = _usage_group() | {
        "problem": row["problem"],
        "run_id": row["run_id"],
        "verified": row["checkpoints"]["2"],
        "stop_reason": endpoint["stop_reason"],
        "roles": {},
        "response_categories": {},
        "tool_calls": Counter(),
        "finish_reasons": Counter(),
        "proof_output_length_finishes": 0,
        "requests_without_response": 0,
    }
    for attempt in attempts:
        _add_attempt(result, attempt)
        role = attempt["role"]
        _add_attempt(result["roles"].setdefault(role, _usage_group()), attempt)
        if row["harness"] != "axprover":
            result["requests_without_response"] += not (attempt.get("response_metadata") or {}).get(
                "response_id"
            )
            continue
        path = (attempt.get("response_metadata") or {}).get("response_file")
        if not path:
            result["requests_without_response"] += 1
            continue
        archive = evidence.read(Path(path))
        _require(
            archive["attempt_id"] == attempt["attempt_id"],
            "Provider archive belongs to another attempt",
        )
        response = archive["response"].get("data", archive["response"])
        tools = response.get("tool_calls") or []
        category = ("search" if tools else "proof_output") if role == "proposer" else role
        _add_attempt(result["response_categories"].setdefault(category, _usage_group()), attempt)
        result["tool_calls"].update(tool["name"] for tool in tools)
        finish = (response.get("response_metadata") or {}).get("finish_reason")
        if finish:
            result["finish_reasons"][finish] += 1
        result["proof_output_length_finishes"] += category == "proof_output" and finish == "length"
    _require(result["tokens"] == ledger["tokens"], "Token totals differ from low endpoint")
    result["known_charge_usd"] = result["charge_usd"]
    result["charge_usd"] = row["initial_reconciled_charge_usd"]
    _require(result["charge_usd"] is not None, "Missing reconciled initial charge")
    difference = Decimal(result["charge_usd"]) - Decimal(result["known_charge_usd"])
    _require(
        difference >= 0 and (difference == 0 or result["missing_charge_calls"]),
        "Reconciled charge differs from complete low ledger",
    )
    result["reconciled_charge_difference_usd"] = str(difference)
    state = evidence.read(Path(row["directory"]) / "controller.json")
    snapshots = state["snapshots"][: endpoint["snapshot_count"]]
    output = snapshots[-1]["verification"].get("output", "") if snapshots else ""
    result["final_snapshot_rejected_for_sorry"] = output in {
        "Disallowed proof syntax: sorry\n",
        "Disallowed proof syntax: sorry\\n",
    }
    return result


def _combine_accounting(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = _usage_group() | {
        "known_charge_usd": "0",
        "roles": {},
        "response_categories": {},
        "tool_calls": Counter(),
        "finish_reasons": Counter(),
        "proof_output_length_finishes": 0,
        "requests_without_response": 0,
        "runs": rows,
    }
    for row in rows:
        for key in (
            "calls",
            "missing_usage_calls",
            "missing_charge_calls",
            "proof_output_length_finishes",
            "requests_without_response",
        ):
            result[key] += row[key]
        for key in ("charge_usd", "known_charge_usd"):
            result[key] = str(Decimal(result[key]) + Decimal(row[key]))
        for key in TOKEN_CATEGORIES:
            result["tokens"][key] += row["tokens"][key]
        for key in ("tool_calls", "finish_reasons"):
            result[key].update(row[key])
        for key in ("roles", "response_categories"):
            for name, source in row[key].items():
                target = result[key].setdefault(name, _usage_group())
                for count in ("calls", "missing_usage_calls", "missing_charge_calls"):
                    target[count] += source[count]
                target["charge_usd"] = str(
                    Decimal(target["charge_usd"]) + Decimal(source["charge_usd"])
                )
                for token in TOKEN_CATEGORIES:
                    target["tokens"][token] += source["tokens"][token]
    tokens = result["tokens"]
    input_total = tokens["input"] + tokens["cache_read"] + tokens["cache_write"]
    output_total = tokens["output"] + tokens["reasoning"]
    result["cache_read_fraction"] = tokens["cache_read"] / input_total if input_total else None
    result["reasoning_output_fraction"] = (
        tokens["reasoning"] / output_total if output_total else None
    )
    return result


def analyze_traces(rows: list[dict[str, Any]], evidence: EvidenceReader) -> dict[str, Any]:
    selected = [row for row in rows if row["model"] == GEMINI]
    _require(len({row["run_id"] for row in selected}) == len(selected), "Duplicate selected run")
    shell_rows = []
    accounting: dict[str, list[dict[str, Any]]] = {"mini": [], "axprover": []}
    for row in sorted(selected, key=lambda row: (row["harness"], row["problem"])):
        directory = Path(row["directory"])
        endpoint = evidence.read(directory / "low-endpoint.json")
        _require(endpoint["phase"] == "low", "Expected low endpoint")
        accounting[row["harness"]].append(_accounting(row, endpoint, evidence))
        if row["harness"] == "mini":
            shell_rows.append(
                {
                    "problem": row["problem"],
                    "run_id": row["run_id"],
                    **_mini_requests(directory, endpoint["ledger"]["attempts"], evidence),
                }
            )
    shell_totals = _shell_counts() | {"responses_without_saved_message": 0}
    for row in shell_rows:
        for key in shell_totals:
            if key == "program_requests":
                for category in PROGRAM_CATEGORIES:
                    shell_totals[key][category] += row[key][category]
            else:
                shell_totals[key] += row[key]
    return {
        "schema_version": 1,
        "scope": "gemini-low",
        "analyzer_sha256": hashlib.sha256(evidence.bytes(Path(__file__))).hexdigest(),
        "mini_shell": {
            "counting_unit": "bash request; program families overlap; counts do not imply execution",
            "categories": list(PROGRAM_CATEGORIES),
            "problems": shell_rows,
            "totals": shell_totals,
        },
        "harnesses": {harness: _combine_accounting(runs) for harness, runs in accounting.items()},
    }
