FROM ubuntu:24.04

ARG COMPARATOR_REV=3927ad383f208ae977c340a91c48ac9b497d2097
ARG EXPORTER_REV=15f6055e299ad5b89345e533cc2192f4cc00f659
ARG LANDRUN_REV=811cfff51ceaf3d9843708aa6d22e9b84ccac8b4
ARG NUMINA_REV=1c9af8a52e715f22fede766425ba3d3b95526132
ARG REPL_REV=0bd10273da8a699074308e2b96ff4ec88020b964

ENV DEBIAN_FRONTEND=noninteractive ELAN_HOME=/opt/elan
ENV PATH=/opt/venv/bin:/opt/elan/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl elan git build-essential libgmp-dev golang-go python3 python3-venv \
      ripgrep jq && rm -rf /var/lib/apt/lists/*
RUN elan toolchain install leanprover/lean4:v4.28.0-rc1 && \
    elan toolchain install leanprover/lean4:v4.33.0 && \
    elan default leanprover/lean4:v4.28.0-rc1
RUN python3 -m venv /opt/venv && pip install --no-cache-dir lean-interact==0.11.5

RUN git init /opt/comparator && cd /opt/comparator && \
    git remote add origin https://github.com/leanprover/comparator && \
    git fetch --depth 1 origin "$COMPARATOR_REV" && git checkout --detach FETCH_HEAD && \
    lake build comparator
RUN git init /opt/exporter && cd /opt/exporter && \
    git remote add origin https://github.com/leanprover/lean4export && \
    git fetch --depth 1 origin "$EXPORTER_REV" && git checkout --detach FETCH_HEAD && \
    printf '%s\n' 'leanprover/lean4:v4.28.0-rc1' > lean-toolchain && \
    lake build
RUN git init /opt/lean-repl && cd /opt/lean-repl && \
    git remote add origin https://github.com/augustepoiroux/repl && \
    git fetch --depth 1 origin "$REPL_REV" && git checkout --detach FETCH_HEAD && \
    lake build
RUN git init /tmp/landrun && cd /tmp/landrun && \
    git remote add origin https://github.com/Zouuup/landrun && \
    git fetch --depth 1 origin "$LANDRUN_REV" && git checkout --detach FETCH_HEAD && \
    go build -o /usr/local/bin/landrun ./cmd/landrun && rm -rf /tmp/landrun
ENV COMPARATOR_LANDRUN=/usr/local/bin/landrun
ENV COMPARATOR_LEAN4EXPORT=/opt/exporter/.lake/build/bin/lean4export

# Fixed fixtures check export compatibility; runtime checks use real Landrun.
RUN python3 - <<'PY'
import json
import os
import subprocess
import tempfile
from pathlib import Path

environment = dict(os.environ)
environment['COMPARATOR_LANDRUN'] = '/opt/comparator/scripts/fake-landrun.sh'
for accepted, solution in (
    (True, 'theorem target (n : Nat) : n = n := rfl\n'),
    (False, 'theorem target (n : Nat) : True := True.intro\n'),
):
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        (project / 'lean-toolchain').write_text('leanprover/lean4:v4.28.0-rc1\n')
        (project / 'lakefile.toml').write_text(
            'name = "format_check"\n[[lean_lib]]\nname = "Challenge"\n'
            '[[lean_lib]]\nname = "Solution"\n'
        )
        (project / 'Challenge.lean').write_text('theorem target (n : Nat) : n = n := by sorry\n')
        (project / 'Solution.lean').write_text(solution)
        (project / 'config.json').write_text(json.dumps({
            'challenge_module': 'Challenge', 'solution_module': 'Solution',
            'theorem_names': ['target'],
            'permitted_axioms': ['propext', 'Quot.sound', 'Classical.choice'],
            'enable_nanoda': False,
        }))
        result = subprocess.run(
            ['lake', 'env', '/opt/comparator/.lake/build/bin/comparator', 'config.json'],
            cwd=project, env=environment, capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        if (result.returncode == 0) != accepted or 'Inductive invalid' in output:
            raise RuntimeError(output)
PY

COPY src/minileanagent /opt/mini/src/minileanagent
COPY paper-experiments/paper_experiments /opt/paper/paper_experiments
COPY paper-experiments/configs /opt/paper/configs
COPY paper-experiments/lean /opt/paper/lean
RUN chmod 755 /opt/paper/lean/comparator-wrapper
ENV PYTHONPATH=/opt/paper:/opt/mini/src

# This project warms LeanInteract; benchmark workspaces are supplied separately.
RUN git init /tmp/numina && cd /tmp/numina && \
    git remote add origin https://github.com/project-numina/numina-lean-agent && \
    git fetch --depth 1 origin "$NUMINA_REV" && git checkout --detach FETCH_HEAD
RUN python3 - <<'PY'
from pathlib import Path
from paper_experiments.prepare import write_project_template
write_project_template(Path('/tmp/numina/leanproblems'), Path('/opt/lean-base'))
PY
RUN rm -rf /tmp/numina && useradd --create-home --uid 1000 --non-unique agent && \
    chown agent:agent /opt/lean-base /opt/lean-base/*
RUN python3 - <<'PY'
import os
from lean_interact.utils import DEFAULT_CACHE_DIR
os.chown(DEFAULT_CACHE_DIR, 1000, 1000)
PY
ENV HOME=/home/agent
USER 1000:1000
RUN cd /opt/lean-base && lake exe cache get
RUN python3 - <<'PY'
from lean_interact import AutoLeanServer, Command, LeanREPLConfig, LocalProject
server = AutoLeanServer(LeanREPLConfig(
    project=LocalProject(directory='/opt/lean-base', auto_build=False),
    local_repl_path='/opt/lean-repl', build_repl=False,
))
response = server.run(Command(cmd='import Mathlib\nexample : True := by trivial'))
if getattr(response, 'has_errors', lambda: True)():
    raise RuntimeError(str(response))
server.kill()
PY
USER root
RUN find /opt /home/agent -type d -name .git -prune -exec rm -rf '{}' '+' && \
    rm -rf /root/.cache /root/go /home/agent/.cache/pip
USER 1000:1000
WORKDIR /tmp
LABEL org.mini-lean.lean="4.28.0-rc1" \
      org.mini-lean.mathlib="5352afccd6866369be9de43f5b7ec47203555f44" \
      org.mini-lean.comparator="3927ad383f208ae977c340a91c48ac9b497d2097" \
      org.mini-lean.exporter="15f6055e299ad5b89345e533cc2192f4cc00f659"
CMD ["bash"]
