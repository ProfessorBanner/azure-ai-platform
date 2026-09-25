# ML Operations Runbook — ml-lifecycle-demo

Operational procedures for the governed ML lifecycle in
`products/ml-lifecycle-demo`. Product design lives in that product's README;
this document is what you follow when something needs doing or has gone wrong.

Every command below is exact and copy-pasteable. Substitute only the values in
`UPPER_CASE`.

## 0. Orientation

| | |
|---|---|
| **Product** | `ml-lifecycle-demo` |
| **Model** | `<CATALOG>.ml_lifecycle_demo.linear_regression_model` |
| **Aliases** | `Candidate` (latest gated fit), `Champion` (the release) |
| **Jobs** | `ml_candidate`, `ml_inference`, `ml_monitor_retrain` |
| **Training sets** | `training_data` (baseline), `retraining_data` (baseline + newly labelled) |
| **Catalogs** | `dev`, `stg`, `prod` |

Set these once per session:

```bash
export CATALOG=prod                     # dev | stg | prod
export MODEL="${CATALOG}.ml_lifecycle_demo.linear_regression_model"
export DATABRICKS_HOST="https://adb-1000000000000004.15.azuredatabricks.net"
```

**Two rules that govern everything here.** Aliases are authoritative mutable
state; model-version tags are immutable provenance. And every operation resolves
an alias **once**, then works from the exact version number — never from the
alias a second time.

## 1. Interpreting monitoring

`ml_monitor_retrain` runs daily at **06:00 UTC** in PROD and appends one row to
`monitoring_history` on every run, healthy or not.

```bash
databricks api post /api/2.0/sql/statements --json '{
  "statement": "SELECT observed_at, model_version, status, retraining_required, retraining_forced, null_rate, drift_score, rmse, r2, reasons FROM '"${CATALOG}"'.ml_lifecycle_demo.monitoring_history ORDER BY observed_at DESC LIMIT 20"
}'
```

If no SQL warehouse is available, read the table from a notebook or a job on
classic compute — Phase 15 deliberately introduces no warehouse.

### What each status means

| `status` | Meaning | Job outcome | Your action |
|---|---|---|---|
| `ok` | All checks passed | green | none |
| `retrain_requested` | Drift or live performance breached a threshold | green | a new Candidate was trained and validated — review and approve it (§3) |
| `critical` | Data-quality failure | **red** | investigate the pipeline (§5). No retraining was requested |

`critical` deliberately does **not** retrain. Retraining on data known to be
broken fits a model to the breakage and launders a pipeline fault into a model
everyone then has to reason about.

### The thresholds

Version-controlled in `products/ml-lifecycle-demo/src/monitoring_logic.py`:

| Check | Threshold | Effect when breached |
|---|---|---|
| Null rate | `MAX_NULL_RATE = 0.01` | critical |
| Missing column / empty table | any | critical |
| Prediction count ≠ batch count | any | critical |
| Feature drift (SMD) | `MAX_FEATURE_DRIFT = 0.2` | retrain requested |
| Live RMSE | `MONITOR_MAX_RMSE = 18.0` | retrain requested |
| Live R² | `MONITOR_MIN_R2 = 0.85` | retrain requested |

Drift is a **standardized mean difference**: how far a feature's mean has moved,
in training standard deviations. `drift_score` is the maximum across features,
never the average — one badly drifted feature is a real problem.

Changing a threshold is a reviewed code change that flows through
`DEV → STG → PROD` like any other.

### Open alerts

```bash
databricks api post /api/2.0/sql/statements --json '{
  "statement": "SELECT raised_at, severity, alert_type, message, model_version FROM '"${CATALOG}"'.ml_lifecycle_demo.monitoring_alerts WHERE acknowledged = false ORDER BY raised_at DESC"
}'
```

**Alerting boundary for Phase 15.** Alerting means exactly three things: durable
`monitoring_alerts` rows, a visible Databricks job failure for critical faults,
and a retraining request recorded in `monitoring_history`. There is **no** email,
Teams or PagerDuty routing, and no webhook. External notification routing belongs
to the later production-operations phase. Until then, someone must look — see
§8.

## 2. Inspecting a Candidate

```bash
# Resolve the alias ONCE and keep the number.
CANDIDATE_VERSION="$(
  products/ml-lifecycle-demo/scripts/model-promotion.sh resolve-alias "$MODEL" Candidate
)"
echo "Candidate is version ${CANDIDATE_VERSION}"

# Full provenance and metrics.
products/ml-lifecycle-demo/scripts/model-promotion.sh describe-version "$MODEL" "$CANDIDATE_VERSION"
```

Verify it is what you think it is before trusting it:

```bash
products/ml-lifecycle-demo/scripts/model-promotion.sh verify-version \
  "$MODEL" "$CANDIDATE_VERSION" \
  --environment "$CATALOG" \
  --git-sha GIT_SHA \
  --require-ready
```

Check its scoring output:

```bash
databricks tables get "${CATALOG}.ml_lifecycle_demo.candidate_predictions" -o json | jq '{full_name, table_type, storage_location}'
```

Every version carries immutable tags: `product`, `environment`, `git_sha`,
`bundle_target`, `training_run_id`, `evaluation_rmse`, `evaluation_r2`,
`registered_as`. `registered_as` is always `candidate` and is never rewritten —
it records how the version was created, not what it is now. **Which version is
Champion is answered only by the alias.**

## 3. Approving a model (moving Champion)

**DEV and STG** move Champion automatically once the candidate is verified. No
action.

**PROD** requires the pipeline. Do not promote PROD by hand except during a
rollback (§6).

The `aiplatform-ml-lifecycle-prod-cd` pipeline requests the `aiplatform-prod`
Environment approval **twice**:

1. **Approval 1 — deploy and evaluate.** Authorises PROD compute to train,
   register a Candidate and prove it scores. Champion does not move.
2. **Approval 2 — release.** Authorises releasing that exact version.

Before granting approval 2, read the `prod-model-candidate` pipeline artifact
attached to the run. It contains `model-release-manifest.json` (the machine-
verified release unit) and `candidate-evidence.txt` (what you are approving).

Confirm the manifest's `candidate_version`, `git_sha` and `status: READY` match
what you expect. The release stage re-validates all of it and re-fetches that
exact number; it never re-resolves the `Candidate` alias after the wait.

Verify afterwards:

```bash
products/ml-lifecycle-demo/scripts/model-promotion.sh verify-alias "$MODEL" Champion VERSION
```

## 4. Forcing a retrain (controlled proof)

`force_retrain` exists only to prove the retraining path end to end without
waiting for real drift. It changes **whether** retraining is requested — never
how the candidate is trained, validated or promoted.

```bash
databricks bundle run -t "$CATALOG" ml_monitor_retrain \
  --params force_retrain=true
```

Run it from `products/ml-lifecycle-demo`. It records `retraining_forced = true`
in `monitoring_history` and raises an `info` / `forced_retrain` alert, so the
audit trail distinguishes an operator action from observed drift.

It does **not** override critical data quality: if the data is broken the job
still fails and no retraining happens.

A forced retrain produces a Candidate like any other. **It does not become
Champion.** Promotion remains §3.

## 5. Failed retraining

Retraining fails in one of three places. Find which from the failed task in the
Databricks run.

**`monitor` failed** — critical data quality. Read the `reasons` column:

```bash
databricks api post /api/2.0/sql/statements --json '{
  "statement": "SELECT observed_at, status, reasons FROM '"${CATALOG}"'.ml_lifecycle_demo.monitoring_history ORDER BY observed_at DESC LIMIT 1"
}'
```

Usual causes: `prepare_data` did not run, `ml_inference` did not run so
`predictions` is stale or empty, or a row-count mismatch. Fix the upstream job,
then re-run `ml_monitor_retrain`. Do not force a retrain to "get past" this.

**`prepare_retraining_data` failed** — the newly labelled observations could not
be trusted. It fails closed on three conditions, all fatal:

- *a batch row has no label* — ground truth is incomplete, and training on the
  labelled subset alone is a biased sample nobody chose;
- *duplicate `row_id` values* — the join is ambiguous and would silently weight
  one observation double;
- *nothing to join* — no ground truth has arrived, so a retrain would only refit
  the original data.

Check that `inference_actuals` has caught up with `batch_input`:

```bash
databricks api post /api/2.0/sql/statements --json '{
  "statement": "SELECT (SELECT count(*) FROM '"${CATALOG}"'.ml_lifecycle_demo.batch_input) AS batch_rows, (SELECT count(*) FROM '"${CATALOG}"'.ml_lifecycle_demo.inference_actuals) AS labelled_rows, (SELECT count(DISTINCT row_id) FROM '"${CATALOG}"'.ml_lifecycle_demo.inference_actuals) AS distinct_labelled"
}'
```

`labelled_rows` below `batch_rows` means ground truth is still arriving — wait,
do not force. `distinct_labelled` below `labelled_rows` means duplicates, which
is an upstream data fault. Champion is untouched throughout.

**`train_register` failed** — the new fit did not clear the training gate
(`MAX_RMSE = 15.0`, `MIN_R2 = 0.90`). This is the gate working. **No version was
registered and no alias moved**, because the gate runs before registration. The
existing Champion is untouched and still serving. Investigate the data before
touching the threshold.

**`validate_candidate` failed** — a version registered and took the `Candidate`
alias, but failed provenance verification or scoring. Champion is untouched.
Inspect it (§2); do not promote it.

In all four cases **production is unaffected** — Champion only ever moves
through §3.

## 6. Rollback

Roll back when a released Champion is misbehaving in PROD.

**Rollback pins an exact previous version. Never roll back to an alias.**
`Candidate` is mutable and may already point at the very model you are backing
out of.

Find the version you want:

```bash
databricks model-versions list "$MODEL" -o json \
  | jq -r '.[] | {version, status, created_at, run_id}'
```

Confirm it before you point production at it:

```bash
products/ml-lifecycle-demo/scripts/model-promotion.sh verify-version \
  "$MODEL" PREVIOUS_VERSION --environment prod --require-ready
```

Roll back:

```bash
databricks registered-models set-alias \
  prod.ml_lifecycle_demo.linear_regression_model \
  Champion \
  PREVIOUS_VERSION
```

Verify, then re-score production with the restored model:

```bash
products/ml-lifecycle-demo/scripts/model-promotion.sh verify-alias \
  prod.ml_lifecycle_demo.linear_regression_model Champion PREVIOUS_VERSION

cd products/ml-lifecycle-demo
databricks bundle run -t prod ml_inference
```

Confirm predictions were rewritten:

```bash
databricks tables get prod.ml_lifecycle_demo.predictions -o json | jq '{full_name, storage_location}'
```

Then raise a change record and open a fix on a branch. A rollback is a manual
divergence from what the pipeline last released: leaving it undocumented means
the next PROD deployment silently overwrites it.

## 7. Pausing and resuming the monitoring schedule

The schedule is deployed to all three environments so the cron is reviewed in
code, and is **PAUSED in DEV and STG, UNPAUSED in PROD**. That is set by the
`monitor_schedule_pause_status` Bundle variable in `databricks.yml` — not by
clicking in the workspace.

To change it permanently, change the variable and let the change flow through
`DEV → STG → PROD`.

To pause PROD **temporarily** during an incident:

```bash
JOB_ID="$(databricks jobs list -o json | jq -r '.[] | select(.settings.name == "ml-lifecycle-demo-monitor-prod") | .job_id')"
echo "Job: ${JOB_ID}"

databricks jobs update "$JOB_ID" --json '{"new_settings": {"schedule": {"quartz_cron_expression": "0 0 6 * * ?", "timezone_id": "UTC", "pause_status": "PAUSED"}}}'
```

Resume by setting `"pause_status": "UNPAUSED"`.

**This is a temporary, out-of-band change.** The next `databricks bundle deploy`
restores whatever `databricks.yml` says. Record it, set a reminder to undo it,
and prefer a code change for anything lasting more than one incident.

## 8. Ownership and escalation

Ownership is stated as **roles**, not individuals. A runbook naming a person
goes stale the first time someone changes team, and the wrong name is worse than
no name — it sends an incident to somebody who no longer has the access to act.
Map these roles to the current on-call rota in your team directory.

| Role | Owns | Engaged when |
|---|---|---|
| **ML Product Owner** | The model's purpose, thresholds and acceptable behaviour. Accountable for whether the product should keep running at all. | A retrain is requested repeatedly; a threshold change is proposed; the model's fitness for purpose is in question. |
| **ML Engineering on-call** | The lifecycle itself: training, evaluation, candidate quality, drift interpretation, retraining outcomes. **First responder for anything in §1–§6.** | Any `critical` monitor run; any failed retrain; any Candidate that will not validate. |
| **Platform Engineering on-call** | The substrate: Databricks workspaces, Unity Catalog, compute policies, deployment identities, pipelines. | Job cannot start; permission or identity errors; catalog, storage or cluster-policy failures; anything that would need a Terraform change. |
| **Business / model-risk approver** | Authorising a specific model version into production, where the business requires it. Holds the `aiplatform-prod` Environment approval. | Approval 2 in §3. Also engaged for any rollback (§6) and any release that diverges from the normal pipeline path. |

The business / model-risk approver role applies **where applicable** — some
models carry regulatory or commercial risk that requires a named accountable
approver, others do not. Where it does not apply, the ML Product Owner holds the
`aiplatform-prod` approval.

### Triage: who takes it first

| Symptom | First responder |
|---|---|
| `status = critical` in `monitoring_history` | ML Engineering on-call |
| Job did not start, or failed before `monitor` | Platform Engineering on-call |
| `train_register` failed the metric gate | ML Engineering on-call |
| Permission, identity or catalog error in any task | Platform Engineering on-call |
| PROD serving a model that looks wrong | ML Engineering on-call, then §6 rollback |
| Repeated retrain requests across days | ML Product Owner |

### Escalate when

- two consecutive `critical` monitor runs — the fault is not transient;
- a retrain is requested on three consecutive runs — the model can no longer
  track the data, and this is an ML Product Owner decision, not another retrain;
- any rollback (§6) — engage the business / model-risk approver, because
  production is now serving something the normal approval path did not release;
- any manual alias change in PROD;
- `prepare_retraining_data` fails repeatedly — ground truth is not arriving, which
  is a data-supply problem, not a model problem.

### Do not, without a reviewed change

- move `Champion` in PROD outside §3 or §6;
- edit model-version tags — they are immutable provenance;
- unpause the DEV or STG schedules permanently;
- loosen a threshold to silence an alert;
- point `--training-table` at anything outside the allowlist.

### The standing gap

Phase 15 has **no push-based alerting**. An unread `monitoring_alerts` table is
the single largest operational risk in this product. Until external routing
exists, the **ML Engineering on-call** must check open alerts on a stated
cadence — daily is proportionate to a daily monitor schedule. External
email/Teams/PagerDuty routing belongs to the later production-operations phase.
