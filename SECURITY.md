# Security policy

The agent has mini-SWE-agent's bash access. A working directory does not restrict its access to
files, credentials, programs, or the network. Run untrusted agents in an operating-system sandbox.

Keep the verification project's `Challenge.lean`, Lake configuration, imports, dependencies, and
toolchain trusted and protected from the agent. The runner copies only the final target into the
reserved `Solution.lean`; it does not create an isolation boundary. Do not compile untrusted
solutions in the verification project before comparator checks them.

Follow [comparator's sandbox requirements](https://github.com/leanprover/comparator#readme), including
its Linux `landrun` requirements. Comparator evaluates proofs; it does not sandbox the agent loop.

Trajectories contain prompts, source code, commands, and output. Review them before sharing.
Report vulnerabilities through a [private GitHub security advisory](https://github.com/TomasOrtega/mini-lean-agent/security/advisories/new).
