"""Exception locations and OS metadata without source text or local variables."""

from __future__ import annotations

import errno
import os
from collections import deque
from typing import Any

from .models import redact_provider_text


def _details(error: BaseException) -> dict[str, Any]:
    frames: deque[dict[str, Any]] = deque(maxlen=64)
    frame_count = 0
    trace = error.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        frames.append(
            {
                "filename": redact_provider_text(code.co_filename),
                "function": redact_provider_text(code.co_name),
                "line": trace.tb_lineno,
            }
        )
        frame_count += 1
        trace = trace.tb_next
    result = {
        "type": type(error).__name__,
        "error": redact_provider_text(str(error)),
        "traceback": list(frames),
        "traceback_truncated": frame_count > len(frames),
    }
    if isinstance(error, OSError):
        result.update(
            errno=error.errno,
            errno_name=errno.errorcode.get(error.errno),
            errno_description=os.strerror(error.errno) if error.errno is not None else None,
            filename=(
                redact_provider_text(os.fsdecode(error.filename))
                if error.filename is not None
                else None
            ),
            filename2=(
                redact_provider_text(os.fsdecode(error.filename2))
                if error.filename2 is not None
                else None
            ),
        )
    return result


def error_details(error: BaseException) -> dict[str, Any]:
    result = _details(error)
    chain = []
    seen = {id(error)}
    current = error
    while True:
        cause = current.__cause__
        following = cause or (None if current.__suppress_context__ else current.__context__)
        if following is None or id(following) in seen or len(chain) == 8:
            result["exception_chain_truncated"] = following is not None
            break
        chain.append(
            {"relation": "cause" if cause is not None else "context", **_details(following)}
        )
        seen.add(id(following))
        current = following
    return {**result, "exception_chain": chain}
