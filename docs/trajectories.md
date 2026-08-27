# Trajectories

mini-lean-agent does not define a trajectory schema. `DefaultAgent` writes mini-SWE-agent's ordinary
trajectory after each step.

After the run, the Lean harness calls the same upstream `save` method with one additional namespaced
object:

```text
info.mini_lean_agent
```

That object records:

- mini-lean-agent and mini-SWE-agent versions
- project and fixed target
- protected declaration and initial type, when supplied
- upstream exit status
- final Lean verification result
- final mini-lean status

The upstream `messages`, `trajectory_format`, model statistics, environment configuration, and other
fields remain unchanged. Use mini-SWE-agent's browser:

```bash
mini-extra inspect <trajectory-file>
```

## Privacy

Trajectories can contain private source code, proofs, shell output, local paths, and secrets printed
by commands. Review and redact them before sharing or using them as training data.
