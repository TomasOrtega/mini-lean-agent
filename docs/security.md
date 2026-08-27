# Security

The model receives bash access through mini-SWE-agent. Use a disposable checkout and a real sandbox.
Do not expose production credentials, private datasets, SSH keys, or writable shared storage.

The Lean verifier is an evaluation boundary, not a security sandbox. It determines whether the
configured target is accepted. It does not restrict arbitrary commands or edits made during the
run.

Trajectories may contain source code, shell output, and secrets printed by commands. Review them
before sharing.
