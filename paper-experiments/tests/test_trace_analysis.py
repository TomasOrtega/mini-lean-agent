import hashlib
import json
from pathlib import Path

import pytest

from paper_experiments.trace_analysis import analyze_traces, classify_shell


class Evidence:
    def __init__(self):
        self.files = {}

    def bytes(self, path, expected=None):
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert expected is None or expected == digest
        self.files[str(path)] = digest
        return data

    def read(self, path, expected=None):
        return json.loads(self.bytes(path, expected))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def attempt(number, charge="0.1", role="proposer", response_id=None):
    return {
        "attempt_id": number,
        "completed": True,
        "charge_usd": charge,
        "role": role,
        "error": None,
        "billable_tokens": {
            "input": 100,
            "cache_read": 200,
            "cache_write": 0,
            "output": 10,
            "reasoning": 20,
        },
        "response_metadata": {"response_id": response_id or f"r{number}"},
    }


def run(tmp_path, attempts, harness="mini", charge="0.2"):
    directory = tmp_path / harness
    tokens = {
        key: sum((a.get("billable_tokens") or {}).get(key, 0) for a in attempts)
        for key in ("input", "cache_read", "cache_write", "output", "reasoning")
    }
    endpoint = {
        "phase": "low",
        "stop_reason": "cost",
        "snapshot_count": 1,
        "ledger": {"calls": len(attempts), "attempts": attempts, "tokens": tokens},
    }
    write(directory / "low-endpoint.json", endpoint)
    write(
        directory / "controller.json",
        {
            "snapshots": [
                {
                    "verification": {
                        "output": "Disallowed proof syntax: sorry\\n",
                        "verified": False,
                    }
                },
                {"verification": {"output": "", "verified": True}},
            ]
        },
    )
    return {
        "directory": str(directory),
        "run_id": f"gemini-3.8-flash--{harness}--A5--r1",
        "model": "gemini-3.8-flash",
        "harness": harness,
        "problem": "A5",
        "initial_reconciled_charge_usd": charge,
        "checkpoints": {"2": False},
    }


def message(response_id, command, tool_id="t1"):
    return {
        "role": "assistant",
        "tool_calls": [{"name": "bash", "command": command}],
        "extra": {
            "response": {"id": response_id},
            "actions": [{"command": command, "tool_call_id": tool_id}],
        },
    }


@pytest.mark.parametrize(
    "command,categories",
    [
        (
            "rg foo Target.lean || grep bar Target.lean; cat Target.lean && lake env lean Target.lean",
            {"text_search", "cat", "lean_check"},
        ),
        ("python3 -c 'print(\"grep foo; cat X\"); print(1 << 2)'", {"python3"}),
        (
            "cat <<'EOF' > Target.lean\ngrep fake\nlake env lean Target.lean\nEOF\npython3 -c 'pass'",
            {"cat", "python3"},
        ),
        ("echo ';' rg fake; echo \"&&\" cat fake; echo '<<' EOF", set()),
        ("diff <(rg lemma A.lean) <(cat B.lean)", {"text_search", "cat"}),
        ("sed -n '1,20p' Target.lean; find . -name '*.lean'; ls", {"sed", "file_discovery"}),
        ("cat <<-EOF\n\tgrep fake\n\tEOF\ntime lake build", {"cat", "lean_check"}),
        ("cat <<A <<B\ngrep fake\nA\ncat fake\nB\nrg real .", {"cat", "text_search"}),
        ("cat 'multi\nline; rg fake'\npython3 -c 'pass'", {"cat", "python3"}),
    ],
)
def test_requested_programs_ignore_payloads_and_count_conditional_commands(command, categories):
    assert classify_shell(command) == (categories, False)


@pytest.mark.parametrize(
    "command,program",
    [("rg 'unclosed", "text_search"), ("python3 -c 'unclosed", "python3"), ("", None)],
)
def test_malformed_request_counts_only_its_leading_program(command, program):
    categories, malformed = classify_shell(command)
    assert categories == ({program} if program else set())
    assert malformed


def test_low_response_ids_ignore_continuation_and_tool_call_duplicates(tmp_path):
    row = run(tmp_path, [attempt(1), attempt(2)])
    first = message("r1", "rg a .; grep b .; cat Target.lean && lake env lean Target.lean")
    first["extra"]["actions"].append(dict(first["extra"]["actions"][0]))
    write(
        Path(row["directory"]) / "trajectory.json",
        {
            "messages": [
                first,
                first,
                message("r2", "python3 -c 'pass'"),
                message("r3", "rg later ."),
            ]
        },
    )
    evidence = Evidence()
    result = analyze_traces([row, {"model": "other"}], evidence)
    shell = result["mini_shell"]["totals"]
    assert shell["bash_requests"] == 2
    assert shell["program_requests"]["text_search"] == 1
    assert shell["program_requests"]["python3"] == 1
    assert shell["cat_and_lean_requests"] == 1
    accounting = result["harnesses"]["mini"]
    assert accounting["calls"] == 2
    assert accounting["charge_usd"] == "0.2"
    assert accounting["tokens"]["cache_read"] == 400
    assert accounting["tokens"]["reasoning"] == 40
    assert accounting["roles"]["proposer"]["calls"] == 2
    assert accounting["runs"][0]["final_snapshot_rejected_for_sorry"] is True
    assert any(p.endswith("trajectory.json") for p in evidence.files)


def test_conflicting_duplicate_response_ids_are_rejected(tmp_path):
    row = run(tmp_path, [attempt(1)], charge="0.1")
    write(
        Path(row["directory"]) / "trajectory.json",
        {"messages": [message("r1", "cat A"), message("r1", "rg B")]},
    )
    with pytest.raises(ValueError, match="Conflicting duplicate response"):
        analyze_traces([row], Evidence())


def test_duplicate_ledger_response_ids_are_rejected(tmp_path):
    row = run(tmp_path, [attempt(1), attempt(2, response_id="r1")])
    write(Path(row["directory"]) / "trajectory.json", {"messages": [message("r1", "cat A")]})
    with pytest.raises(ValueError, match="Duplicate ledger response"):
        analyze_traces([row], Evidence())


def test_ax_roles_length_finish_and_missing_error_response(tmp_path):
    attempts = [
        attempt(1),
        attempt(2),
        attempt(3, role="memory_processor"),
        attempt(4, role="reviewer"),
        attempt(5, charge=None),
    ]
    attempts[-1].update(billable_tokens=None, error="provider unavailable", response_metadata={})
    row = run(tmp_path, attempts, "axprover", "0.4")
    for a, tools, finish in zip(
        attempts,
        [[{"name": "search_lean_search_tool"}], [], [], []],
        ["length", "length", "stop", "length"],
        strict=False,
    ):
        path = Path(row["directory"]) / f"response-{a['attempt_id']}.json"
        write(
            path,
            {
                "attempt_id": a["attempt_id"],
                "response": {
                    "data": {"tool_calls": tools, "response_metadata": {"finish_reason": finish}}
                },
            },
        )
        a["response_metadata"]["response_file"] = str(path)
    endpoint = Evidence().read(Path(row["directory"]) / "low-endpoint.json")
    endpoint["ledger"]["attempts"] = attempts
    write(Path(row["directory"]) / "low-endpoint.json", endpoint)
    result = analyze_traces([row], Evidence())["harnesses"]["axprover"]
    assert result["calls"] == 5
    assert result["charge_usd"] == "0.4"
    assert result["missing_usage_calls"] == 1
    assert result["requests_without_response"] == 1
    assert result["proof_output_length_finishes"] == 1
    assert result["finish_reasons"]["length"] == 3
    assert result["tool_calls"] == {"search_lean_search_tool": 1}
    assert result["response_categories"]["search"]["calls"] == 1
    assert result["response_categories"]["proof_output"]["calls"] == 1
    assert result["response_categories"]["memory_processor"]["calls"] == 1
    assert result["response_categories"]["reviewer"]["calls"] == 1
    assert result["tokens"]["reasoning"] == 80


def test_rejects_token_totals_that_disagree_with_low_ledger(tmp_path):
    row = run(tmp_path, [attempt(1)], charge="0.1")
    path = Path(row["directory"]) / "low-endpoint.json"
    endpoint = Evidence().read(path)
    endpoint["ledger"]["tokens"]["reasoning"] += 1
    write(path, endpoint)
    with pytest.raises(ValueError, match="Token totals"):
        analyze_traces([row], Evidence())


def test_reconciled_charge_must_agree_with_complete_low_ledger(tmp_path):
    row = run(tmp_path, [attempt(1)], charge="0.3")
    write(Path(row["directory"]) / "trajectory.json", {"messages": [message("r1", "cat A")]})
    with pytest.raises(ValueError, match="Reconciled charge"):
        analyze_traces([row], Evidence())


def test_missing_provider_request_does_not_create_a_bash_action(tmp_path):
    attempts = [attempt(1), attempt(2, charge="0")]
    attempts[1].update(response_metadata={}, billable_tokens=None, error="provider unavailable")
    row = run(tmp_path, attempts, charge="0.1")
    write(
        Path(row["directory"]) / "trajectory.json", {"messages": [message("r1", "cat Target.lean")]}
    )
    result = analyze_traces([row], Evidence())
    assert result["mini_shell"]["totals"]["bash_requests"] == 1
    assert result["harnesses"]["mini"]["calls"] == 2
    assert result["harnesses"]["mini"]["requests_without_response"] == 1
    assert result["harnesses"]["mini"]["missing_usage_calls"] == 1
