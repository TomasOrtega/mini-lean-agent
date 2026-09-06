import errno
import json

from paper_experiments.diagnostics import error_details


def test_os_error_preserves_both_paths_and_redacts_credentials(monkeypatch):
    secret = "fake-path-credential"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    error = OSError(errno.ERANGE, secret, b"/bundle/source.trace", None, f"/other/{secret}")
    details = error_details(error)
    assert details["filename"] == "/bundle/source.trace"
    assert details["filename2"] == "/other/[REDACTED]"
    assert details["errno"] == errno.ERANGE
    assert details["errno_description"]
    assert secret not in json.dumps(details)


def test_traceback_does_not_read_source_or_include_source_literals(monkeypatch):
    import linecache

    monkeypatch.setattr(linecache, "getline", lambda *args: (_ for _ in ()).throw(AssertionError()))
    try:
        exec(
            compile("private = 'not-in-receipt'\nraise ValueError('failure')", "source.py", "exec")
        )
    except ValueError as error:
        details = error_details(error)
    assert details["traceback"][-1] == {"filename": "source.py", "function": "<module>", "line": 2}
    assert "not-in-receipt" not in json.dumps(details)


def test_implicit_context_and_cycles_are_bounded():
    outer, inner = RuntimeError("outer"), ValueError("inner")
    outer.__context__ = inner
    inner.__context__ = outer
    details = error_details(outer)
    assert [entry["type"] for entry in details["exception_chain"]] == ["ValueError"]
    assert details["exception_chain"][0]["relation"] == "context"
    assert details["exception_chain_truncated"] is True
    outer.__suppress_context__ = True
    assert error_details(outer)["exception_chain"] == []
