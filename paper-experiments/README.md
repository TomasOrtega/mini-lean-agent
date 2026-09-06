# Paper experiments

This directory contains the Putnam 2025 experiments for mini-Lean-agent. We retain
the upstream mini-SWE-agent and AxProverBase loops, then add shared accounting,
isolated workspaces, bounded workers, and independent proof verification. Run the
commands below from the repository root.

## Results

We report one selected attempt per problem and harness. The completed mini-Lean-agent
campaigns use `max` reasoning effort and a $50 allowance per problem:

| Model | Verified | Model charges | API calls |
| --- | ---: | ---: | ---: |
| GPT-6 Astra | 12/12 | $15.367271 | 106 |
| Claude Fable 5.1 | 11/12 | $120.294990 | 133 |

Astra solved every problem for less than $3 each. Fable solved every problem except
A5, where it reached the spending cap at $50.104620. Both campaigns used two workers,
started fresh attempts, and recorded complete accounting without infrastructure
failures. We did not import pilot results or another campaign's proofs.

The original Gemini 3.8 Flash comparison solved 7/12 problems with mini-Lean-agent
and 4/12 with AxProverBase at the $2 stopping cap; Opus 4.5 solved 0/12 at $2 and $5.
The paper credits a proof from the final request dispatched below a stopping cap,
including AxProverBase's A1 proof at $2.00150925. Strict scoring gives AxProverBase
3/12. The reported comparison uses the completed B2 mini and B6 Ax runs. Their raw
registrations remain unchanged. The low-budget costs per verified proof are $2.09
for mini-Lean-agent and $4.71 for AxProverBase; all reported model runs cost $251.823705.

## Protocol and isolation

All campaigns preserve the same Numina challenge statements and use Lean/Mathlib
`v4.28.0-rc1`. [configs/putnam2025.json](configs/putnam2025.json) pins the challenges,
Mathlib commit `5352afccd6866369be9de43f5b7ec47203555f44`, evaluator, and dependencies.
Each attempt allows 200 API calls, or 300 on A5/B6. Retries and all model roles share
that count and the spending ledger.

| Campaign | Runs | Dollar and active-time limits |
| --- | ---: | --- |
| `original` | 24 Gemini + 12 Opus | Gemini: $2 / 2 hours; its four A5/B6 runs may continue to cumulative $50 / 24 hours. Opus: $5 / 2 hours. |
| `astra` | 12 mini | $50 / 24 hours each; checkpoints at $2, $5, $10, $20, $50. |
| `fable` | 12 mini | Same limits and checkpoints as Astra. |

The nominal allocations are $300 for the original campaign and $600 each for Astra
and Fable. The last in-flight request can cross a stopping cap. We charge failed
requests and unsuccessful attempts; unavailable charges remain unknown. OpenRouter
receipts determine spending, including upstream charges for BYOK requests. The dated
model and price files record provider routes and token-price estimates.

The host sends model requests to OpenRouter. Agent and evaluator containers use
`--network=none`. AxProverBase alone receives its native six-result LeanSearch tool
through a host client restricted to `POST https://leansearch.net/search`; the client
archives requests and responses. Its Mathlib release matches the operator-confirmed
LeanSearch corpus release. Mini-only campaigns do not initialize retrieval.

The evaluator preserves the supplied prefix and accepts only plain proved `theorem`
helpers in the editable suffix. It checks the target against the original challenge
and verifies every helper under the `propext`, `Quot.sound`, and `Classical.choice`
axiom allowlist. Verification uses trusted dependencies and ignores candidate build
artifacts. Source-policy and Comparator checks share a 600-second snapshot limit,
which counts toward the attempt's active time.

## Prepare a trusted environment

Use Docker on Linux or Docker Desktop on macOS; the containers need Landlock support.
Each worker reserves an agent and an evaluator, each with two CPUs and 6 GiB RAM,
without swap. Two workers require 26 GiB of available Docker memory; assign at least
28 GiB to Docker Desktop and leave enough host memory for macOS. The runner checks
capacity before dispatch. The original profile defaults to one worker; Astra and
Fable require two. Use the same resource profile for audit, smoke, and registration.

Choose `astra` or `fable` below. For the original comparison, set `PAPER_CAMPAIGN=original`
and use `configs/models.json`, `configs/prices.json`, and `configs/resources.json`.
Set `OPENROUTER_API_KEY` in the host environment before the smoke and run commands.

```bash
PAPER_ROOT="$PWD/.mini-lean-agent/reproduction"
PAPER_CAMPAIGN=astra
PAPER_MODELS="paper-experiments/configs/$PAPER_CAMPAIGN-models.json"
PAPER_PRICES="paper-experiments/configs/$PAPER_CAMPAIGN-prices.json"
PAPER_RESOURCES="paper-experiments/configs/$PAPER_CAMPAIGN-resources.json"
PAPER_PROJECT="$PAPER_ROOT/project"
PAPER_BUNDLE="$PAPER_ROOT/bundle"
PAPER_RUN="$PAPER_ROOT/$PAPER_CAMPAIGN"
mkdir -p "$PAPER_ROOT"
git clone https://github.com/project-numina/numina-lean-agent.git "$PAPER_ROOT/numina"
git -C "$PAPER_ROOT/numina" checkout 1c9af8a52e715f22fede766425ba3d3b95526132
PAPER_SOURCE="$PAPER_ROOT/numina/leanproblems"
docker build -f paper-experiments/Dockerfile -t mini-lean-paper .
PAPER_IMAGE=$(docker image inspect mini-lean-paper --format '{{.Id}}')
PAPER_REVISION=$(git rev-parse HEAD)
uv run --project paper-experiments python paper-experiments/experiment.py project \
  --source "$PAPER_SOURCE" --output "$PAPER_PROJECT"
docker run --rm --user root --cpus 2 --memory 6g --memory-swap 6g \
  --mount type=bind,src="$PAPER_PROJECT",dst=/work -w /work \
  "$PAPER_IMAGE" bash -c 'lake exe cache get && lake build Mathlib'
uv run --project paper-experiments python paper-experiments/experiment.py prepare \
  --source "$PAPER_SOURCE" --bundle "$PAPER_BUNDLE" \
  --dependencies "$PAPER_PROJECT/.lake/packages" \
  --image "$PAPER_IMAGE" --mini-revision "$PAPER_REVISION" \
  --evaluator-revision "$PAPER_REVISION"
uv run --project paper-experiments python paper-experiments/experiment.py audit \
  --bundle "$PAPER_BUNDLE" --output "$PAPER_ROOT/audit.json" \
  --resources "$PAPER_RESOURCES"
```

The trusted setup container downloads dependencies. Complete `lake build Mathlib`
before preparation: verification needs its generated C/IR files, build metadata,
and ProofWidgets assets as well as `.olean` files. Preparation validates revisions,
materializes permitted links, and hashes the dependency inventory. The audit checks
real valid and invalid proofs and provisions a verified read-only Docker cache.

## Run a campaign

```bash
uv run --project paper-experiments python paper-experiments/experiment.py smoke \
  --campaign "$PAPER_CAMPAIGN" --bundle "$PAPER_BUNDLE" \
  --output "$PAPER_ROOT/$PAPER_CAMPAIGN-smoke" --audit-report "$PAPER_ROOT/audit.json" \
  --models "$PAPER_MODELS" --prices "$PAPER_PRICES" --resources "$PAPER_RESOURCES"
uv run --project paper-experiments python paper-experiments/experiment.py register \
  --campaign "$PAPER_CAMPAIGN" --bundle "$PAPER_BUNDLE" --output "$PAPER_RUN" \
  --audit-report "$PAPER_ROOT/audit.json" \
  --smoke-report "$PAPER_ROOT/$PAPER_CAMPAIGN-smoke/smoke-report.json" \
  --models "$PAPER_MODELS" --prices "$PAPER_PRICES" --resources "$PAPER_RESOURCES" \
  --seed 2025
uv run --project paper-experiments python paper-experiments/experiment.py run \
  --experiment "$PAPER_RUN" --phase all
```

The smoke check uses a trivial non-Putnam theorem. Astra/Fable smoke limits are $2,
six calls, and 30 minutes. Registration requires successful audit and smoke receipts
and freezes settings, source hashes, SDK versions, and run order. Keep that code and
environment unchanged while executing or resuming a registration.

The queue saves ledgers, trajectories, candidate snapshots, and terminal endpoints
incrementally. Repeating `run` continues unfinished work and skips terminal runs;
it does not create fresh attempts. `--phase all` keeps each designated continuation
in its worker slot. Queue waiting consumes no active time. Keep the computer awake
and connected; a process interruption cannot guarantee preservation of an in-flight
provider response.

For sequential campaigns, [run_after.py](run_after.py) accepts `--config` with a
frozen handoff JSON containing predecessor/successor paths, twelve run IDs each,
immutable input hashes, and smoke/register/run command arrays. It checks saved
progress every 30 minutes and starts the successor only after the predecessor exits
successfully with all attempts terminal. It preserves progress locally and does not
retry failed paid setup automatically.

## Reports and checks

For an original Gemini/Opus registration, create trace annotations and export its
strict budget report:

```bash
PAPER_ORIGINAL=/absolute/path/to/original-experiment
uv run --project paper-experiments python paper-experiments/experiment.py annotations \
  --experiment "$PAPER_ORIGINAL" --output "$PAPER_ROOT/annotations.csv"
uv run --project paper-experiments python paper-experiments/experiment.py report \
  --experiment "$PAPER_ORIGINAL" --output "$PAPER_ROOT/report" \
  --annotations "$PAPER_ROOT/annotations.csv"
```

Complete the annotation CSV using its generated rubric and observed trace evidence.
Blank fields remain unannotated. Reports validate frozen snapshots and endpoints,
include failures in cost totals and denominators, and leave cost per proof undefined
when no proof succeeds. Setup, smoke checks, retrieval, and local compute stay separate
from benchmark model charges.

Run unit tests without model calls:

```bash
uv run --project paper-experiments pytest paper-experiments/tests
```

Set `PAPER_LEAN` for native Lean parser tests; also set `PAPER_COMPARATOR`,
`PAPER_EXPORTER`, and `PAPER_LANDRUN` for exporter/kernel integration tests. The real
Docker audit remains a prerequisite for benchmark execution.

## Generate the final paper tables

[make_tables.py](make_tables.py) validates frozen inputs and writes exactly two files:
`results.tex` contains the metrics and seven table macros used by the paper;
`results.json` contains selected results, campaign costs, and compact reproducibility
metadata. The generator checks raw evidence hashes without copying the file inventory
into its output. It does not run models or modify archives. Keep the local records
available; trajectories, credentials, and dependency caches do not ship in Git.

```bash
PAPER_RESULTS=.mini-lean-agent/paper-remaining-2026-09-04/final-report
PAPER_RETRY=.mini-lean-agent/diagnostic-B6-Ax-2026-09-05
PAPER_REPLACEMENT=.mini-lean-agent/replacement-B2-mini-2026-09-05
uv run --project paper-experiments python paper-experiments/make_tables.py \
  --combined "$PAPER_RESULTS/combined.json" \
  --annotations "$PAPER_RESULTS/annotations/pooled-trace-statistics.json" \
  --annotation-provenance "$PAPER_RESULTS/annotations/annotation-provenance.json" \
  --diagnostic "$PAPER_RETRY" \
  --diagnostic-supplement "$PAPER_RETRY/runs/gemini-3.8-flash--axprover--B6--r1/billing-reconciliation-supplement-20260905-final.json" \
  --diagnostic-supplement-sha256 953955eaf717573e92e16f15df5c1e49e0d9d55cb6cb1887298383696a58d376 \
  --replacement "$PAPER_REPLACEMENT" \
  --replacement-manifest-sha256 0f6bf34e6d24ee72c448dcc19f9a1a796ca544935c8138e9fa37ff5e0728b15c \
  --replacement-annotation "$PAPER_REPLACEMENT/analysis/mini-lean-B2-mini-annotation.json" \
  --replacement-annotation-sha256 096a4482c296c674bf591b5be79793496f40477dcdc037c2e122dd32f422967d \
  --astra-experiment .mini-lean-agent/paper-astra-2026-09-05/experiment \
  --fable-experiment .mini-lean-agent/paper-fable-2026-09-05/experiment \
  --budget-policy last-dispatch --output .mini-lean-agent/final-paper-tables
```

The output directory must be new. `last-dispatch` applies the paper's final-request
allowance only at a stopping cap; interior checkpoints and call/time limits remain
strict. `--budget-policy strict` reconstructs the original budget-qualified scoring.
The generator selects the completed B6 Ax retry for both low and extended results,
while preserving its archived diagnostic registration. Reported costs count each
selected attempt once, including continuations, and exclude smoke tests and unselected
attempts. Copy the two files into `generated/mini-lean-agent`; load `results.tex` once
and use its `\Paper...Table` commands inside the paper's table floats.
