# ml-lifecycle-demo

One real, minimal, end-to-end classical ML lifecycle on the Azure AI Platform.
It is deployed through the platform's promotion chain — **DEV → STG → PROD**,
same source and same commit SHA in every environment — and production deployment
stays behind the Azure DevOps Environment approval.

The contract this product is held to is
[`docs/platform/product-contract.md`](../../docs/platform/product-contract.md).

## Ownership

| | |
|---|---|
| **Owner / team** | Platform engineering (Phase 15) |
| **Contact** | _TODO: reachable channel_ |
| **Monitoring owner** | _TODO: who responds when a run fails_ |
| **Unity Catalog schema** | `ml_lifecycle_demo` in the `dev`, `stg` and `prod` catalogs |

## What it does

Two jobs per environment, split along the approval boundary.

**`ml_candidate`** — one shared classic cluster, `max_concurrent_runs: 1`:

```text
prepare_data  →  train_register  →  validate_candidate
```

`prepare_data` generates a deterministic synthetic regression dataset — seeded,
never downloaded — and overwrites two governed tables. `train_register` trains a
`LinearRegression` model, logs parameters, metrics and the model to a
Bundle-managed MLflow experiment, enforces the evaluation threshold, and only
then registers the version, writes its provenance tags, and assigns the
**`Candidate`** alias. **It never touches `Champion`.** `validate_candidate`
resolves `Candidate` once, pins the exact version number, verifies the version's
environment and Git SHA, scores `batch_input` with that exact numbered version,
and writes `candidate_predictions`.

**`ml_inference`** — its own policy-governed classic cluster:

```text
batch_inference
```

Resolves `Champion` once, pins the number, scores with that exact version, and
writes `predictions`. In PROD this runs only after a human has approved the
release. It mutates no registry metadata.

**`ml_monitor_retrain`** — its own cluster, `max_concurrent_runs: 1`, scheduled
daily at 06:00 UTC:

```text
monitor → retraining_required? ─[true]─→ prepare_retraining_data
                               │             → train_register → validate_candidate
                               └[false]→ (job ends, green)
```

`monitor` measures data quality, feature drift and live performance against
delayed ground truth, appends a `monitoring_history` row and any
`monitoring_alerts` rows, and publishes a `retraining_required` task value. The
condition task branches on it, so the expensive path runs only when the monitor
asked for it — and the reasoning is already durable before the branch is taken.

`prepare_retraining_data` is what makes this **retraining** rather than
refitting. It joins the batch features to the ground truth that has since
arrived and combines those newly labelled observations with the original
training set, writing `retraining_data`. Refitting the unchanged `training_data`
would produce a near-identical model, burn a cluster, and leave the drift that
triggered the retrain unaddressed.

Retraining reuses the **same** `train_register` and `validate_candidate` tasks as
`ml_candidate`: same metric gate, same provenance tags, same `Candidate` alias.
The only difference is which governed table it fits on. **A retrained model does
not become Champion.** Promotion stays the separate, approved decision.

Governed artefacts, per environment `<CATALOG>`:

| Artefact | Name |
|---|---|
| Training table | `<CATALOG>.ml_lifecycle_demo.training_data` |
| Batch input table | `<CATALOG>.ml_lifecycle_demo.batch_input` |
| Candidate scoring output | `<CATALOG>.ml_lifecycle_demo.candidate_predictions` |
| Production predictions | `<CATALOG>.ml_lifecycle_demo.predictions` |
| Delayed ground truth | `<CATALOG>.ml_lifecycle_demo.inference_actuals` |
| Retraining set | `<CATALOG>.ml_lifecycle_demo.retraining_data` |
| Monitoring history | `<CATALOG>.ml_lifecycle_demo.monitoring_history` |
| Monitoring alerts | `<CATALOG>.ml_lifecycle_demo.monitoring_alerts` |
| Registered model | `<CATALOG>.ml_lifecycle_demo.linear_regression_model` |
| Aliases | `Candidate`, `Champion` |

`candidate_predictions` is deliberately a different table from `predictions`. A
candidate is an unapproved model; letting it write the table production
consumers read would make *validating* a candidate and *shipping* it the same
act.

`inference_actuals` holds `row_id` and `label` **and no features**. It is the
delayed ground truth monitoring joins against, and the absence of feature columns
is the structural guarantee that the scoring path cannot read its own answers —
a model that saw them would score perfectly and monitor perfectly, and the
monitoring would be worthless.

Model serving is deliberately out of scope for this step.

## Code promotion versus model promotion

**The Git commit is what moves between environments. The model artefact is not.**

DEV, STG and PROD each train their own model, against their own catalog's data,
and register it in their own catalog and storage. Nothing copies a `.pkl` from
DEV into PROD.

This is a deliberate choice, and it is the opposite of what "promote the model"
usually implies:

- **The environments are genuinely isolated.** Each has its own workspace, its
  own storage account, its own Unity Catalog catalog, and its own managed
  identity. Copying an artefact across that boundary would mean granting one
  environment read access into another's storage — precisely the isolation the
  platform exists to maintain.
- **Each environment's model matches its own data.** A model fitted to DEV data
  is not the model PROD should serve. Promoting the *recipe* (the commit) rather
  than the *result* (the weights) is what makes PROD's model correct for PROD.
- **Reproducibility is proven, not assumed.** Because training is deterministic
  and seeded, the same commit produces a comparable model in each environment.
  If STG's metrics diverge from DEV's, that is a real signal about the
  environment or its data — a signal that copying the artefact would hide.

So the promotion chain `DEV → STG → PROD` promotes the commit, and each
environment independently re-derives and re-proves its own model from it.

## Candidate versus Champion

Two aliases on the same registered model, with different meanings:

| | `Candidate` | `Champion` |
|---|---|---|
| **Set by** | `train_register`, every run | a promotion decision |
| **Means** | passed its metric gate | this is the release |
| **Read by** | `validate_candidate` | `batch_inference` |
| **In DEV / STG** | set automatically | follows the verified Candidate automatically |
| **In PROD** | set automatically | moves only after a human approves one exact version |

### Aliases are state; tags are provenance

The registry holds two kinds of information, and the product keeps them strictly
apart:

| | Aliases | Model-version tags |
|---|---|---|
| **Nature** | authoritative **mutable** state | **immutable** provenance |
| **Answer** | "what is Champion *now*?" | "what *was* this version?" |
| **Written by** | the promotion decision, only | `train_register`, at registration, once |
| **Rewritten** | yes — that is their job | never |

Aliases are mutable pointers, so every consumer resolves an alias **once**,
records the resulting integer, and does all subsequent work against
`models:/<model>/<version>` — never `models:/<model>@<alias>`. Between resolving
an alias and using it, a concurrent run could move it, and the job would then
score, or the pipeline promote, an artefact nobody evaluated.
`max_concurrent_runs: 1` makes that race unlikely; pinning the integer makes it
impossible to go unnoticed.

Each version carries provenance tags written at registration — `product`,
`environment`, `git_sha`, `bundle_target`, `training_run_id`, `evaluation_rmse`,
`evaluation_r2`, `registered_as` — so a promotion pipeline can independently
verify *which* artefact it is about to release rather than trusting the alias.

`registered_as` is always `candidate`, in every environment, and **is never
rewritten**. It states how the version was created; it is not a release status.
There is deliberately no `registered_as=champion`: a tag that got rewritten on
promotion would make the registry hold two competing answers to "what is
Champion?", and the tag copy could silently go stale. **The alias is the only
answer to that question.** Correspondingly, `batch_inference` mutates no
registry metadata at all — it reads the alias and writes data.

## The two PROD approvals

PROD requests the `aiplatform-prod` Environment approval **twice**, for two
different questions:

1. **Approval 1 — deploy and evaluate.** Authorises spending PROD compute:
   deploy the Bundle, train, register a Candidate, and prove it scores. The
   exact numbered version and its evidence (`describe-version` output plus the
   candidate predictions table) are printed to the log and published as the
   `prod-candidate-evidence` pipeline artifact. **Champion does not move.**
2. **Approval 2 — release.** Authorises releasing the specific artefact that
   evidence describes. Only now does `Champion` move, and production inference
   runs.

Collapsing these into one approval would mean approving a model before anyone
had seen how it scored.

#### The release manifest

The release unit crosses the approval boundary as an **immutable pipeline
artifact**, not a variable. Approval 1's stage writes
`model-release-manifest.json` and publishes it as `prod-model-candidate`:

```json
{
  "product": "ml-lifecycle-demo",
  "full_model_name": "prod.ml_lifecycle_demo.linear_regression_model",
  "catalog": "prod",
  "schema": "ml_lifecycle_demo",
  "candidate_version": "7",
  "git_sha": "<Build.SourceVersion>",
  "run_id": "<MLflow run>",
  "source": "<artifact URI>",
  "status": "READY"
}
```

It is written only after the version passes full verification, so it can never
record an unverified claim. A stage output variable was the obvious alternative
and is worse: it is invisible after the run, easy to get subtly wrong in a
`stageDependencies` expression, and proves nothing about what the approver saw.

After approval 2, the release stage downloads that artifact **from the current
run**, validates every field — product, model name, PROD catalog, numeric
version, `READY` status, and `git_sha` against `Build.SourceVersion` — then
re-fetches and re-verifies that exact numbered version against the workspace
before `Champion` moves. `Champion` is checked again after inference.

**The `Candidate` alias is never re-resolved after approval.** Doing so would
release whatever happened to be Candidate at approval time rather than the
version the approver read evidence for. If the manifest is absent, malformed, or
disagrees with the workspace, the release fails closed — it is never repaired by
falling back to the alias.

## The promotion helper

`scripts/model-promotion.sh` holds the one implementation of alias resolution,
provenance verification, promotion and alias confirmation, so DEV, STG and PROD
call it rather than each carrying its own copy of the same `jq`. It is
fail-closed throughout and covered by `tests/shell/test-model-promotion.sh`,
which mocks the Databricks CLI — no workspace, no credential, no model promoted.

```bash
./scripts/model-promotion.sh resolve-alias    <model> Candidate   # prints the version
./scripts/model-promotion.sh verify-version   <model> 7 --environment prod --git-sha <sha> --require-ready
./scripts/model-promotion.sh describe-version <model> 7
./scripts/model-promotion.sh write-manifest   <model> 7 --environment prod --git-sha <sha> --output <path>
./scripts/model-promotion.sh verify-manifest  <path> --model <model> --environment prod --git-sha <sha>
./scripts/model-promotion.sh promote          <model> 7           # the only mutation
./scripts/model-promotion.sh verify-alias     <model> Champion 7
```

`resolve-alias` and `verify-manifest` print **only** the version number on
stdout, with diagnostics on stderr, so a pipeline can capture them with `$(...)`.
`promote` is the only subcommand that changes anything.

The helper writes no model-version tags, and does not need to: tags are
immutable provenance, written once at registration by the MLflow client on the
cluster. (The Databricks CLI could not do it anyway — `model-versions update`
documents "Currently only the comment of the model version can be updated", and
`entity-tag-assignments` covers catalogs, schemas, tables, columns and volumes,
not models.)

### The evaluation gate

The threshold lives in `src/ml_logic.py` (`MAX_RMSE`, `MIN_R2`), in versioned
source rather than per-environment configuration: a model that is not good
enough for PROD is not good enough for DEV either. A failing model raises, which
fails the Databricks task, the job, and the pipeline stage — and because the
gate runs **before** registration, a failing model never becomes a registered
version and never receives an alias.

## Monitoring and controlled retraining

Three checks, all decided by pure logic in `src/monitoring_logic.py` with
version-controlled thresholds. Spark computes the aggregates; the judgements are
unit-tested on a laptop.

| Check | Threshold | Breach means |
|---|---|---|
| Required columns, non-empty tables, prediction count = batch count | exact | **critical** |
| Null rate | `MAX_NULL_RATE = 0.01` | **critical** |
| Feature drift (standardized mean difference, max across features) | `MAX_FEATURE_DRIFT = 0.2` | retrain requested |
| Live RMSE against delayed actuals | `MONITOR_MAX_RMSE = 18.0` | retrain requested |
| Live R² against delayed actuals | `MONITOR_MIN_R2 = 0.85` | retrain requested |

Drift uses a standardized mean difference — how far a feature's mean moved, in
training standard deviations — chosen over KS or PSI because it is explainable.
"`feature_1`'s average moved 0.4 training standard deviations" is a sentence an
on-call engineer can act on at 3am; monitoring nobody can reason about gets
muted, and a muted monitor is worse than none.

The monitor bounds are deliberately **looser** than the training gate
(`MAX_RMSE 15.0` / `MIN_R2 0.90`): the training gate judges a fresh fit on
held-out data, these judge a deployed model on data that has moved on. A test
asserts they never cross — if they did, every run would demand a retrain the
training gate would then reject, in an expensive loop.

**Critical data quality fails the job and requests no retraining.** Retraining on
data known to be broken would fit a model to the breakage and launder a pipeline
fault into a model everyone then has to reason about.

### The retraining set

`retraining_data` = `training_data` + every batch row whose label has since
arrived, deduplicated by `row_id` and sorted by it.

Two determinism rules, both tested:

- **On a `row_id` collision the newly labelled observation wins.** Ground truth
  that arrived later is the more recent statement about that row, so a corrected
  label actually takes effect.
- **The result is sorted by `row_id`.** Training splits on a seed, and an
  unstable row order would make that seed meaningless — two runs of the same
  commit would fit different models and their metrics would not be comparable.

`training_data` is never modified. It stays the stable baseline that
cross-environment metric comparison depends on.

The join fails closed on a batch row with no label (training on the labelled
subset alone is a biased sample nobody chose), on duplicate `row_id`s (an
ambiguous join that would silently double-weight an observation), and on an
empty result (nothing new to learn).

Which table training reads is a job parameter constrained to an allowlist —
`training_data` or `retraining_data` — and it takes a **short** name only. The
catalog and schema always come from the running environment, so the parameter
cannot be pointed at another product's data or another environment's.

### Forced retraining

`force_retrain=true` is a job parameter that exists only to prove the retraining
path without waiting for real drift:

```bash
databricks bundle run -t prod ml_monitor_retrain --params force_retrain=true
```

It changes only **whether** retraining is requested. The candidate is trained,
gated, tagged and validated by exactly the same path, and does not become
Champion. The run records `retraining_forced = true` and raises an
`info` / `forced_retrain` alert, so an operator action is never mistaken for
observed drift. It does not override critical data quality.

### The alerting boundary

For Phase 15, alerting means exactly three things:

1. durable `monitoring_alerts` rows (`acknowledged = false` on write);
2. a visible Databricks **job failure** for critical faults;
3. a retraining request recorded in `monitoring_history`.

There is **no** email, Teams, PagerDuty or webhook routing, and no SQL warehouse.
External notification routing belongs to the **later production-operations
phase**. Until it exists, an unread `monitoring_alerts` table is the largest
operational risk in this product — a named person must check it on a stated
cadence. See [`docs/platform/ml-operations-runbook.md`](../../docs/platform/ml-operations-runbook.md).

### Schedule

The schedule is deployed to all three environments so the cron is reviewed in
code, and paused everywhere except PROD via the `monitor_schedule_pause_status`
Bundle variable:

| Target | Schedule | `pause_status` |
|---|---|---|
| `dev` | `0 0 6 * * ?` UTC | `PAUSED` |
| `stg` | `0 0 6 * * ?` UTC | `PAUSED` |
| `prod` | `0 0 6 * * ?` UTC | `UNPAUSED` |

A monitor firing daily in DEV and STG would start clusters and retrain models
nobody asked about. Resuming one is a documented, reversible operator action.

## Operations

[`docs/platform/ml-operations-runbook.md`](../../docs/platform/ml-operations-runbook.md)
covers monitoring interpretation, Candidate inspection, model approval, failed
retraining, rollback, pausing and resuming schedules, and escalation — with exact
CLI commands.

Rollback always pins an **exact previous version**, never an alias:

```bash
databricks registered-models set-alias \
  prod.ml_lifecycle_demo.linear_regression_model \
  Champion \
  PREVIOUS_VERSION
```

`Candidate` is mutable and may already point at the very model you are backing
out of.

## Layout

```text
ml-lifecycle-demo/
├── databricks.yml            # Bundle: variables and one target per environment
├── resources/
│   ├── experiment.yml        # MLflow experiment, one per environment
│   └── job.yml               # ml_candidate, ml_inference, ml_monitor_retrain
├── scripts/
│   └── model-promotion.sh    # Alias resolution, provenance, promotion
├── src/
│   ├── ml_logic.py           # Pure logic: data, naming, metrics, thresholds,
│   │                         #   aliases, version URIs, provenance
│   ├── monitoring_logic.py   # Pure logic: quality, drift, performance, decision
│   ├── prepare_data.py       # ml_candidate task 1
│   ├── prepare_retraining_data.py  # ml_monitor_retrain — newly labelled set
│   ├── train_register.py     # ml_candidate task 2 — registers a Candidate
│   ├── validate_candidate.py # ml_candidate task 3 — proves it scores
│   ├── batch_inference.py    # ml_inference — scores with Champion
│   └── monitor.py            # ml_monitor_retrain task 1 — observe and decide
├── tests/
│   ├── unit/                 # No credentials, no compute, no Spark
│   └── shell/                # Promotion helper, with the CLI mocked
├── pyproject.toml
├── uv.lock
└── README.md
```

`src/ml_logic.py` holds every decision the lifecycle makes and imports no Spark,
no MLflow client and no Databricks SDK. That is what lets the whole test suite
run on a laptop in seconds.

## Environment values

Workspace host, catalog, compute policy and run-as identity are environment-owned
and injected per Bundle target. They are never hard-coded in `src/`: the job
passes `CATALOG` and `SCHEMA` to the workload as cluster environment variables,
and `resolve_config()` fails closed if either is absent or unexpected.

The MLflow experiment is a Bundle **resource**, not something the training code
creates. Its ID is passed to `train_register` as a task parameter
(`${resources.experiments.ml_lifecycle_experiment.id}`), so tracking has the same
governed lifecycle as the job that writes to it.

## Runtime dependencies

`mlflow==3.15.2` and `scikit-learn==1.9.0`, pinned in three places that must
agree: `pyproject.toml`, `uv.lock`, and the `libraries:` block in
`resources/job.yml` that installs them on the job cluster. CI's `uv sync
--frozen` is what keeps the first two honest.

## Infrastructure prerequisite

The `ml_lifecycle_demo` schema and its grants are Terraform-owned
(`infrastructure/modules/databricks_product_uc`, instantiated in each
environment root). This product needs `CREATE_MODEL` on that schema to register
a model, which is why its module instances set `enable_create_model = true`. No
`ALL_PRIVILEGES`, no `CREATE_SCHEMA`.

The ML pipelines do **not** depend on the Terraform pipelines. Terraform
provisions durable prerequisites; the product pipelines deploy product
resources. Their coupling is the platform contract, not pipeline completion.

## Validating a change

Cheapest sufficient check first — see
[`docs/platform/engineering-workflow.md`](../../docs/platform/engineering-workflow.md)
for the proof levels.

```bash
uv sync --frozen
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src tests
uv run pytest tests -v
./tests/shell/test-model-promotion.sh
databricks bundle validate -t dev
```

Both test suites run in CI (`azure-pipelines/ml-lifecycle-demo-ci.yml`); a
failure in either fails the build.

Deployment happens through the platform pipelines, not from a workstation:
`ml-lifecycle-demo-ci.yml`, then `-dev-cd`, `-stg-cd`, `-prod-cd` in
`azure-pipelines/`.
