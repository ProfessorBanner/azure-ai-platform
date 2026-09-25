# Phase 19.2a — Databricks agent capability and evidence discovery

Read-only discovery for the ML platform operations agent. **No Azure or
Databricks resource was created, modified or deleted; no LLM was invoked; no
warehouse was started; no job was run; no alias was changed.**

Observed **2026-09-03** against profile `aiplatform-dev`
(`adb-1000000000000002.12`, catalog `dev`, schema `ml_lifecycle_demo`).

Live observations carry an expiry. Anything below marked *(observed)* is a fact
about that workspace on that date, not a permanent property of the design.

---

## 1. What Phase 15 actually provides

Source: `products/ml-lifecycle-demo/`, `docs/platform/ml-operations-runbook.md`.

### Governed naming (`src/ml_logic.py`)

| Constant | Value |
|---|---|
| `PRODUCT_SCHEMA` | `ml_lifecycle_demo` |
| `GOVERNED_CATALOGS` | `dev`, `stg`, `prod` (sandbox deliberately absent) |
| `REGISTERED_MODEL_NAME` | `linear_regression_model` |
| Aliases | `Candidate` (set by training), `Champion` (release pointer) |
| Training gate | `MAX_RMSE = 15.0`, `MIN_R2 = 0.90` |
| Determinism | `RANDOM_STATE = 42` — identical data and fit across environments |

Tables the product declares: `training_data`, `batch_input`, `predictions`,
`candidate_predictions`, `inference_actuals`, `retraining_data`,
`monitoring_history`, `monitoring_alerts`.

### Monitoring contract (`src/monitoring_logic.py`)

Thresholds are version-controlled, not per-environment:

| Check | Threshold | Effect |
|---|---|---|
| Null rate | `MAX_NULL_RATE = 0.01` | critical |
| Missing column / empty table | any | critical |
| Prediction count ≠ batch count | any | critical |
| Feature drift (SMD) | `MAX_FEATURE_DRIFT = 0.2` | retrain requested |
| Live RMSE | `MONITOR_MAX_RMSE = 18.0` | retrain requested |
| Live R² | `MONITOR_MIN_R2 = 0.85` | retrain requested |

`drift_score` is the **maximum** standardized mean difference across features,
never the mean. Statuses: `ok`, `retrain_requested`, `critical`. A `critical`
run deliberately requests **no** retraining.

`build_history_row` defines the `monitoring_history` columns the agent would
read: `observed_at`, `environment`, `model_name`, `model_version`,
`batch_row_count`, `prediction_row_count`, `actuals_row_count`, `null_rate`,
`drift_score`, `rmse`, `r2`, `status`, `retraining_required`,
`retraining_forced`, `reasons`, `git_sha`.

`build_alert_rows` defines `monitoring_alerts`: `raised_at`, `severity`,
`alert_type`, `message`, `model_version`, `git_sha`, `acknowledged`.
Alert types: `data_quality`, `feature_drift`, `model_performance`,
`forced_retrain`.

### Runbook

`docs/platform/ml-operations-runbook.md` (363 lines, 9 sections) is the
authoritative operational text: status interpretation, thresholds, candidate
inspection, promotion, forced retrain, failed retraining, rollback, schedule
pausing, ownership and escalation. It is repository-resident and versioned —
there is **no UC volume** holding runbooks *(observed: `volumes list` empty)*.

Two constraints it states that bound the agent:

- **The standing gap.** Phase 15 has no push-based alerting; an unread
  `monitoring_alerts` table is named as the largest operational risk.
- **Do not, without a reviewed change**: move `Champion` in PROD outside §3/§6,
  edit model-version tags, unpause DEV/STG schedules, loosen a threshold to
  silence an alert, or point `--training-table` outside the allowlist.

---

## 2. Live inventory *(observed 2026-09-03, `aiplatform-dev`)*

### Unity Catalog

`dev.ml_lifecycle_demo` holds **five** tables, not eight:

| Table | Columns | Last updated |
|---|---|---|
| `training_data` | `row_id`, `feature_0..2`, `label` (all DOUBLE) | 2026-08-27 12:30 |
| `batch_input` | `row_id`, `feature_0..2` | 2026-08-27 12:30 |
| `predictions` | `row_id`, `feature_0..2`, `prediction:ARRAY`, `model_name`, `model_version`, `scored_at:TIMESTAMP` | 2026-08-27 13:00 |
| `candidate_predictions` | same shape as `predictions` | 2026-08-27 16:53 |
| `inference_actuals` | `row_id`, `label` | 2026-08-28 16:30 |

**Absent: `monitoring_history`, `monitoring_alerts`, `retraining_data`.**

`prediction` is typed `ARRAY`, not `DOUBLE` — an adapter must unwrap it.

Schema grants: the deploy service principal `2222aaaa-…` holds
`CREATE_MODEL, CREATE_TABLE, MODIFY, SELECT, USE_SCHEMA`. No agent identity
exists yet.

### MLflow

One experiment: `1800196305929734`,
`/Users/2222aaaa-…/.bundle/ml-lifecycle-demo/dev/ml-lifecycle-demo-dev`.

**Seven runs**, all `FINISHED`, 2026-08-27 12:31 → 2026-08-28 16:31. Every run
records `rmse = 10.077918` and `r2 = 0.992569` — **identical across all seven**,
which is the intended consequence of `RANDOM_STATE = 42` on unchanged training
data. Run tags carry `product`, `environment`, `bundle_target`, `git_sha`.

### Registered model

`dev.ml_lifecycle_demo.linear_regression_model`, **7 versions**, all `READY`.
Aliases *(observed)*: `candidate` → v7 and `champion` → v7. Unity Catalog
returns alias names **lower-cased**; the code writes `Candidate`/`Champion`.

Version tags are complete provenance — `bundle_target`, `environment`,
`evaluation_r2`, `evaluation_rmse`, `git_sha`, `product`, `registered_as`,
`training_run_id` — and `model_metrics` carries `rmse`/`r2` directly.

**The `databricks model-versions get` CLI omits `tags`; the REST endpoint
`/api/2.1/unity-catalog/models/{name}/versions/{v}` returns them.** An adapter
must use the REST path or `MlflowClient`, not that CLI shape.

### Jobs

| Job | ID | Runs |
|---|---|---|
| `ml-lifecycle-demo-candidate-dev` | 143329357046400 | 3 (all SUCCESS, 27–28 Aug) |
| `ml-lifecycle-demo-monitor-dev` | 149357288315370 | **none, ever** |
| `ml-lifecycle-demo-inference-dev` | 318506115613197 | **none, ever** |

The monitor schedule is `PAUSED` in DEV by design (`databricks.yml`), which is
why no monitoring tables exist.

### Compute and serving

- **SQL warehouse**: `Serverless Starter Warehouse` (`0ea28cf3f254fa85`), Small,
  serverless, `auto_stop_mins: 10`, **STOPPED**. Workspace-default, not
  product-provisioned — the runbook's "Phase 15 deliberately introduces no
  warehouse" remains true of the *product*.
- **Serving endpoints**: 14 foundation-model endpoints ready, including
  `databricks-claude-opus-5`, `databricks-claude-sonnet-5`,
  `databricks-gte-large-en`. No custom endpoint.
- **Databricks Apps**: feature available, **zero apps deployed**.
- **System tables enabled**: `system.lakeflow` (`job_run_timeline`,
  `job_task_run_timeline`, `jobs`, `job_tasks`), `system.mlflow`
  (`runs_latest`, `run_metrics_history`, `experiments_latest`),
  `system.serving`, `system.ai_gateway`, `system.access`, `system.query`.

---

## 3. Evidence contract

For **"Why did model X degrade during the last seven days?"**

Every tool result carries the envelope: `source_type`, `source_identifier`,
`observed_at`, `query_window`, `data`, `limitations`.

| # | Evidence field | Why it is required |
|---|---|---|
| E1 | Model identity resolved to `catalog.schema.name` | A question about "model X" is unanswerable until X is a governed name |
| E2 | Version(s) in the window, and which held `Champion` | Degradation is a property of a *served* version, not of a name |
| E3 | Monitoring time series: `observed_at`, `status`, `rmse`, `r2`, `drift_score`, `null_rate` | The only authoritative record of live behaviour over time |
| E4 | Threshold values in force | A metric without its threshold cannot establish *degradation* |
| E5 | Drifted feature names | Distinguishes input-distribution change from model fault |
| E6 | Data-quality counts: `batch_row_count`, `prediction_row_count`, `actuals_row_count` | A `critical` run invalidates E3 computed on the same data |
| E7 | Alert history: `raised_at`, `severity`, `alert_type`, `acknowledged` | Establishes *when* it started and whether anyone saw it |
| E8 | Version change events in the window (registration, alias moves) | The leading candidate cause; a metric shift at a version boundary is a different story from a drift |
| E9 | Training-time metrics of the served version | Separates "was never good enough" from "was good and degraded" |
| E10 | Job run outcomes for monitor/inference in the window | A missing metric may mean a failed job, not a healthy model |
| E11 | Runbook guidance matching the observed status | Turns a diagnosis into a sanctioned next action |

**Degradation is only assertable when E3 crosses E4.** Without both, the correct
answer is `insufficient_evidence` — not a hedged diagnosis.

**Causation rule.** A cause may be reported as *supported* only when it is
anchored to an identified source row **and** a mechanism the runbook or the
monitoring contract already recognises. Two metrics moving together is
`correlation_only` and belongs in `recommended_investigations[]`, never in
`likely_causes[]`.

---

## 4. Source map

| Field | Authoritative source | `source_type` | Access | Status |
|---|---|---|---|---|
| E1 | UC registered model | `unity_catalog_model` | `MlflowClient` / UC REST | **available** |
| E2 | UC model versions + aliases | `unity_catalog_model_version` | UC REST (not the CLI) | **available** |
| E3 | `{catalog}.ml_lifecycle_demo.monitoring_history` | `unity_catalog_table` | SQL Statement Execution | **MISSING in DEV** |
| E4 | `src/monitoring_logic.py` constants | `repository_source` | repo read | **available** |
| E5 | `monitoring_history.reasons` (+ drifted feature names) | `unity_catalog_table` | SQL | **MISSING in DEV** |
| E6 | `monitoring_history` row counts | `unity_catalog_table` | SQL | **MISSING in DEV** |
| E7 | `{catalog}.ml_lifecycle_demo.monitoring_alerts` | `unity_catalog_table` | SQL | **MISSING in DEV** |
| E8 | UC model version `created_at` + alias state | `unity_catalog_model_version` | UC REST | **partial** — current aliases only, no alias history |
| E9 | MLflow run metrics / version tags | `mlflow_run` | `MlflowClient` | **available but non-discriminating** (all 7 runs identical) |
| E10 | Jobs API runs, or `system.lakeflow.job_run_timeline` | `job_run` / `system_table` | Jobs API (no compute) / SQL | **available** |
| E11 | `docs/platform/ml-operations-runbook.md` | `repository_document` | repo read | **available** |

### Availability tally

The Status column above resolves to four states, not two. Stated explicitly so
the arithmetic cannot drift:

| State | Count | Fields |
|---|---|---|
| Available and discriminating | 5 | E1, E2, E4, E10, E11 |
| Available but non-discriminating | 1 | E9 — present, but identical across all 7 versions, so it cannot separate "degraded" from "healthy" |
| Partial | 1 | E8 — current alias state only; no alias history |
| Blocked (source absent) | 4 | E3, E5, E6, E7 |
| **Total** | **11** | |

"Six available" therefore counts E9, whose availability is real but useless for
the bounded question. Only **five** fields both exist and discriminate, and none
of those five can establish degradation on its own — E3 crossing E4 is the
only construction that can, and E3 is blocked. This is why the DEV outcome is
`insufficient_evidence` rather than a weak diagnosis.

Secondary sources, if primary monitoring is absent: `predictions` +
`inference_actuals` joined on `row_id` and windowed by `scored_at` could yield
live RMSE/R². **This is a computation, not an observation** — the agent would be
manufacturing the very metric it is meant to read. If used at all it must be
labelled `derived`, never `monitoring_history`.

---

## 5. Gap register

No fixtures were manufactured in this subphase. These are stated, not filled.

| # | Gap | Impact | Owner |
|---|---|---|---|
| **G1** | `monitoring_history` does not exist in DEV | **Blocking for a genuine 19.2c degradation proof.** E3, E5, E6 unavailable; the primary question cannot be answered from authoritative data | requires a monitor run (a mutation — out of scope here) |
| **G2** | `monitoring_alerts` does not exist in DEV | E7 unavailable; "when did it start / was it seen" unanswerable | same as G1 |
| **G3** | All 7 model versions have identical `rmse`/`r2` | E9 cannot discriminate; no offline degradation signal exists in the registry | inherent to `RANDOM_STATE = 42` — a fixture concern for 19.2b, not a defect |
| **G4** | Monitor and inference jobs have never run in DEV | No `job_run` evidence; `predictions` predates `inference_actuals` | schedule `PAUSED` by design |
| **G5** | No alias *history* — only current alias state | E8 partial; "which version was Champion on day N" is not answerable from UC | design limitation; would need an audit source |
| **G6** | Reading any UC table requires starting the stopped warehouse | Any live table read has a cost and a cold start | accepted; declare in `limitations` |
| **G7** | No agent identity or grants exist | 19.2e least-privilege identity is unprovisioned | 19.2e |
| **G8** | `prediction` column is `ARRAY`, not `DOUBLE` | Adapter must unwrap before any arithmetic | 19.2c |
| **G9** | UC lower-cases alias names vs `Candidate`/`Champion` in code | Case-sensitive alias comparison will silently miss | 19.2c |
| **G10** | `retraining_data` absent | Retraining-path evidence unavailable | consequence of G4 |
| **G11** | `products/ml-lifecycle-demo/mlflow.db` (852K) is untracked and un-ignored | A local MLflow store could be committed by accident | hygiene, unrelated to 19.2 |

**G1/G2 are the central finding.** DEV cannot currently support the live
degradation proof 19.2c specifies. The honest outcomes are: run the monitor job
in DEV (an explicit, approved mutation), point 19.2c at a catalog where
monitoring rows exist, or accept that the live proof demonstrates *correct
abstention* — which is itself a required negative case.

---

## 6. Proposed 19.2b structure

Matches the requested layout. Pure Python, offline, no Databricks or LLM call.

```
products/ml-platform-operations-agent/
├── pyproject.toml
├── src/ml_platform_operations_agent/
│   ├── domain.py      # EvidenceEnvelope, ToolResult, AgentAnswer, enums
│   ├── evidence.py    # the E1–E11 contract and completeness rules
│   ├── policy.py      # tool allowlist; read-only enforcement; refusal taxonomy
│   ├── prompts.py     # versioned, no environment literals
│   ├── agent.py       # bounded loop; provider-agnostic decision port
│   ├── tools/         # models.py runs.py monitoring.py runbooks.py
│   ├── adapters/      # fake.py (19.2b) | databricks.py (19.2c)
│   └── serving.py     # deferred to 19.2e
├── evaluation/        # cases.jsonl, scorers.py, cli.py
└── tests/
```

**Dependencies (19.2b only):** `pydantic` for the envelope and answer schema;
`pytest`, `ruff`, `mypy` as dev. **No** `mlflow`, `databricks-sdk`,
`langgraph`, `openai-agents`, `mcp` or `databricks-agents` — those arrive in
19.2c/19.2d behind the adapter boundary, which is what keeps the 19.2b gate
("no Databricks or LLM calls") structurally true rather than merely observed.

Two design points the discovery forces:

1. **`domain.py` must model absence as a first-class outcome.** G1/G2 mean
   `insufficient_evidence` is the *common* path in DEV, not an edge case.
2. **The envelope's `source_identifier` must be a resolvable address** — a full
   `catalog.schema.table`, a `run_id`, a version number, or a repo path with a
   heading anchor. Identifier validity is a 19.2d gate at 100%, so a free-text
   source string would fail it by construction.

Reuse from Phase 18 (`products/platform-engineering-assistant`): the registry /
policy / bounded-loop / approval shape is proven and should be followed, not
reinvented. Whether to import it or mirror it is a 19.2b decision that depends
on whether this product can share the `openai` pin.

---

## 7. Blockers

| Blocker | Blocks | Needs |
|---|---|---|
| **G1/G2** — no monitoring data in DEV | 19.2c live degradation proof | a decision: run the monitor (mutation, needs approval), retarget, or prove abstention |
| **G7** — no agent identity | 19.2e | Terraform / grant design |
| **G6** — warehouse stopped | any live table read | accept cold start and cost |

None of these block **19.2b**, which is offline by definition.

---

## 8. What Phase 19.2b did with this

Added `products/ml-platform-operations-agent/` — the offline controlled core.
Every adapter constraint above is modelled in `domain.py` and asserted by tests;
`adapters/fake.py` carries 18 synthetic scenarios and a `DEV_SNAPSHOT` constant
reproducing the observations in section 2, so the 19.2c live outcome
(`insufficient_evidence`) is predicted offline before anything is called.

Two findings above changed the design rather than merely being recorded:

- **G3** (identical metrics across all versions) is why
  `permits_degradation_finding` refuses to accept registry or run metrics as a
  degradation signal at all. Accepting them would answer the question with data
  that has no bearing on it.
- **G1/G2** (absent monitoring tables) are why `EvidenceAbsence` is a return
  value rather than an exception. Absence is the ordinary DEV case, and an
  interface that raised on it would put the common path in an `except` branch.

The product mirrors the Phase 15 thresholds rather than importing them, and a
test reads the real constants out of `monitoring_logic.py` to prove they have
not drifted.

**G11** is resolved: `products/ml-lifecycle-demo/mlflow.db` now has a
narrow, exact-path `.gitignore` rule. The file itself was preserved and never
inspected, staged or moved.

Sections 1–7 remain a record of the 2026-09-03 read-only discovery and were not
revised by later work.

### A discovery-method correction found in 19.2c

Section 2 records aliases via `databricks registered-models get
--include-aliases`. Building the live adapter on the REST endpoint exposed a
second lossy default of the same family as the CLI's omitted tags:

**`GET /api/2.1/unity-catalog/models/{name}` returns an EMPTY alias list unless
`include_aliases=true` is passed.** Not an error — a successful, plausible,
wrong response. Observed live on 2026-09-04: the first run of the adapter
reported `aliases: []` for a model that has two.

The observation in section 2 is unaffected (the CLI flag supplied them), but
any future reader reaching for the REST endpoint would have been misled. Both
traps are now documented in the product README and each has a regression test.

**19.2c live outcome, 2026-09-04, `aiplatform-dev`:** the bounded question
returned `insufficient_evidence` exactly as predicted here, citing
`dev.ml_lifecycle_demo` — the schema listing — as evidence that
`monitoring_history` is absent. No workspace mutation occurred; the SQL
warehouse remained `STOPPED`.

**19.2d, 2026-09-04:** one deterministic evaluation of the 19 synthetic cases
ran against Managed MLflow experiment `4496022372296182`
(`/Shared/phase19-2-ml-platform-operations-agent-dev`), producing 19 traces,
154 spans and 354 assessments with all 14 scorers at 1.0 — zero model calls and
zero LLM judges. A third environment defect surfaced there, in the same family
as the two above: **MLflow's Databricks auth forces a token refresh that cannot
write to the macOS Keychain from a non-interactive subprocess, and MLflow then
sends no credential at all**, dropping traces on a background thread while the
run reports progress. See the product README.
