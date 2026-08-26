# Security

The `bash` tool is arbitrary code execution. Project-root path checks apply to structured Lean-file
operations, not to shell commands.

## Safe deployment pattern

1. create a disposable checkout for exactly one task
2. run under a low-privilege account in a container or VM
3. mount only required source and caches
4. provide only the model credential needed for that run
5. block cloud metadata endpoints and unnecessary network access
6. cap CPU, memory, process count, disk, and wall time outside the Python process
7. archive the diff and trajectory, then destroy the environment

The built-in command timeout is not a complete sandbox. A process may alter files, spawn children,
consume resources before termination, or communicate over the network.

See the repository's `SECURITY.md` for vulnerability reporting.
