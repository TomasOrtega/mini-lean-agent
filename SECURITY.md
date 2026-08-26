# Security policy

## Arbitrary command execution is a core capability

mini-lean-agent gives a language model access to a `bash` tool. The model can read, modify, or delete
anything available to the process account, invoke network clients, inspect environment variables,
and run arbitrary programs. The project root is a working-directory convention, not a security
boundary.

Use a disposable checkout and an operating-system sandbox such as a container, VM, restricted HPC
job, or dedicated low-privilege account. Do not expose production credentials, SSH keys, cloud
metadata endpoints, private datasets, or writable shared storage.

API keys should be passed through provider-specific environment variables. Trajectories contain
prompts, model outputs, commands, and command output. Review them before sharing because tools may
print secrets or private source code.

## Supported versions

Security fixes are applied to the latest released minor version.

## Reporting a vulnerability

Open a private GitHub security advisory in the repository. Do not include live credentials, private
proof corpora, or exploit details in a public issue.
