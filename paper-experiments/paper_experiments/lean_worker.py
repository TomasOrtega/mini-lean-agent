"""Persistent LeanInteract service inside the network-isolated Lean container."""

from __future__ import annotations

import contextlib
import json
import socket
import sys
import time
from pathlib import Path

SOCKET = "/tmp/paper-lean-interact.sock"


def serve(workspace: str) -> None:
    from lean_interact import AutoLeanServer, Command, FileCommand, LeanREPLConfig, LocalProject

    with contextlib.redirect_stdout(sys.stderr):
        project = LocalProject(directory=workspace, auto_build=False)
        server = AutoLeanServer(
            LeanREPLConfig(project=project, local_repl_path="/opt/lean-repl", build_repl=False),
            max_total_memory=4,
        )
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(SOCKET)
            listener.listen(1)
            try:
                while True:
                    connection, _ = listener.accept()
                    with connection, connection.makefile("rwb") as stream:
                        try:
                            data = json.loads(stream.readline())
                            command = FileCommand(**data) if "path" in data else Command(**data)
                            response = server.run(command)
                            result = {"response": response.model_dump(mode="json")}
                        except Exception as error:
                            result = {"error": f"{type(error).__name__}: {error}"}
                        stream.write(json.dumps(result).encode() + b"\n")
                        stream.flush()
            finally:
                server.kill()
                Path(SOCKET).unlink(missing_ok=True)


def request() -> None:
    with socket.socket(socket.AF_UNIX) as connection:
        for attempt in range(100):
            try:
                connection.connect(SOCKET)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if attempt == 99:
                    raise
                time.sleep(0.1)
        with connection.makefile("rwb") as stream:
            stream.write(sys.stdin.buffer.readline())
            stream.flush()
            sys.stdout.buffer.write(stream.readline())


def main() -> None:
    if sys.argv[1] == "serve":
        serve(sys.argv[2])
    elif sys.argv[1] == "request":
        request()
    else:
        raise ValueError("Expected serve or request")


if __name__ == "__main__":
    main()
