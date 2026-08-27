# Security policy

mini-lean-agent gives a language model the same bash capability as mini-SWE-agent. The project root
is a working-directory convention, not a security boundary. The model can read environment
variables, change files, invoke network clients, and run arbitrary programs available to the
process account.

Use disposable checkouts and an operating-system sandbox such as a container, VM, restricted batch
job, or dedicated low-privilege account. Do not expose production credentials, SSH keys, cloud
metadata endpoints, private datasets, or writable shared storage.

The final verifier prevents a model from claiming success for a configured target that does not
compile or contains common proof placeholders. When a declaration name is supplied, it also checks
that declaration's elaborated type before and after the run. It is not a general security sandbox and
does not protect imports, unrelated declarations, other files, or trusted axioms. Benchmark evaluators
should inspect the final diff and apply task-specific policies.

Trajectories contain prompts, model outputs, commands, and command output. Review them before
sharing.

Report vulnerabilities through a private GitHub security advisory. Do not publish live credentials
or private proof corpora in an issue.
