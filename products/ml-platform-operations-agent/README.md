# ML Platform Operations Agent (Phase 19.2b)

A bounded, read-only agent that answers one question:

> **Why did model X degrade during the last seven days?**

and returns exactly one of `degraded`, `not_degraded` or `insufficient_evidence`
— with citations an operator can resolve, or an explicit statement of what was
missing.

**Phase 19.2b is entirely offline.** No Databricks call, no Azure call, no LLM
invocation, no clock read. Live adapters arrive in 19.2c behind the protocols in
`adapters/protocols.py`.

---

## What is real, what is synthetic

This distinction governs the whole product, so it is stated first.

| Category | Where it lives | Status |
|---|---|---|
| **Real observations, 2026-09-03** | `docs/phase19-2a-evidence-discovery.md`, and `adapters/fake.DEV_SNAPSHOT` | Read-only facts about the `aiplatform-dev` workspace on that date. They carry an expiry. |
| **Synthetic fixtures** | `adapters/fake.SCENARIOS` (18 scenarios) | Invented. Shaped like the Phase 15 schemas so the agent meets realistic data. Every entry carries `synthetic=True`, asserted by a test. **No number in there is an observation.** |
| **Live proof** | Phase 19.2c | Not yet performed. |
| **Not implemented** | tracing, `mlflow.genai.evaluate`, the Databricks App | 19.2d and 19.2e. |

The DEV workspace has **no monitoring tables** (19.2a, gaps G1/G2), so the
correct live answer to the bounded question today is `insufficient_evidence`.
Positive degradation is proven against fixtures, and labelled as such.

---

## Architecture

```
question ──► policy.screen_request ──► REFUSED (before any tool runs)
                    │ allowed
                    ▼
             config.parse_model_name ──► REFUSED (unknown_model)
                    │ governed
                    ▼
    agent._gather : DIAGNOSTIC_TOOL_ORDER, a CONSTANT
        inspect_registered_model ─┐
        get_model_performance     │  each authorised by policy.authorise
        get_drift_metrics         ├─► adapters/protocols.py ──► fake | databricks (19.2c)
        get_recent_model_runs     │
        search_ml_operations_…   ─┘
                    │ ToolResult: reference | absence
                    ▼
    evidence.classify_status  ← config.monitoring_thresholds
                    │
                    ▼
    evidence.demote_unsupported_causes ──► Diagnosis
```

### Ownership

| Concern | Owner | Why there |
|---|---|---|
| What counts as *degraded* | `evidence.permits_degradation_finding` | One place, pure, testable without a workspace |
| What counts as *supported* | `domain.LikelyCause` + `evidence.demote_unsupported_causes` | The constructor makes an unsupported assertion unrepresentable |
| What may be asked | `policy.screen_request` | Runs before any tool, so a refusal costs nothing |
| What may be called | `tools.build_registry` + `policy.authorise` | Registry built in code, frozen, no state-changing entry |
| Which addresses may be read | `config.EvidenceScope` | Allow-list, never assembled from request input |
| Where evidence comes from | `adapters/protocols.py` | The only boundary a live call can cross |

### Four decisions worth knowing

**The tool loop is a constant, not a plan.** `DIAGNOSTIC_TOOL_ORDER` is fixed
and nothing a tool *returns* can cause another tool to run. That is the
structural answer to prompt injection in monitoring rows and runbooks: injected
text cannot reach tool selection because tool selection never reads tool output.
A dynamic planner would be more impressive and strictly worse — the only thing
it could add here is a path from retrieved text to control flow.

**Absence is a value, not an exception.** `monitoring_history` genuinely does
not exist in DEV, so absence is the *ordinary* case. `EvidenceAbsence` flows
through to `insufficient_evidence`. An interface that could only express absence
by raising would put the common path in an `except` branch.

**Identifiers are parsed, not trusted.** Every `source_identifier` must match
the grammar its `source_type` demands — `catalog.schema.table`, a 32-hex run id,
a repository-relative path. A citation an operator cannot resolve is
indistinguishable from one the agent invented, and 19.2d gates this at 100%, so
it is enforced at construction rather than detected afterwards.

**An unmeasured model is not a healthy one.** A series whose metrics are all
NaN would otherwise fall through to "no metric crossed its threshold" and report
`not_degraded`. That is the dangerous direction: an operator reading
`not_degraded` stops looking. At least one finite value is required before any
conclusion about breaching is drawn.

---

## The rules

A **degradation finding** is permitted only when all four hold:

1. a monitoring time series exists;
2. it *covers* the requested window — observations near both ends, no gap over
   two days (a seven-day question answered from one row is worse than no answer,
   because it looks like an answer);
3. a version-controlled threshold is available;
4. a metric crosses it.

Anything else is `insufficient_evidence`, with the blocking reason recorded in
`limitations` so the abstention is useful rather than a shrug.

A **cause is `supported`** only when it cites an exact `EvidenceReference`, that
evidence contains the observation, and an approved runbook supplies the
mechanism. Otherwise it is `correlation_only` and appears under
`recommended_investigations`, phrased as a question to answer.

The `supported_drift` and `correlation_only_drift` fixtures carry **identical
metrics** and differ only in whether the runbook mechanism is present. That pair
is the product's central claim, and a test asserts the difference.

---

## Read-only guarantees

The agent cannot retrain, promote, move an alias, run a job or write a table —
**because no such code exists in this package**, not because a policy declines.
`tests/test_config_and_hygiene.py` asserts that no function in `tools.py`
contains a mutating verb, and `test_policy.py` asserts the registry holds no
state-changing tool.

A state-changing tool is **denied**, never escalated for approval: an approval
path would imply the action becomes possible once approved.

Prohibited requests are refused **before any tool runs** — a refusal that
queried the workspace first came too late to have prevented anything.

Offline-ness is structural: `mlflow`, `databricks-sdk`, `openai`, `langgraph`,
`httpx` and friends are not installed, and a test parses every module's AST to
assert none is imported.

---

## Layout

```
pyproject.toml                      pydantic only at runtime
src/ml_platform_operations_agent/
  domain.py       contracts, identifier grammars, invariants
  config.py       governed catalogs, table allow-list, mirrored thresholds
  evidence.py     coverage, the degradation gate, cause routing, confidence
  policy.py       request screening, tool authorisation, untrusted content
  tools.py        registry + the five bounded read-only tools
  agent.py        the fixed loop and diagnosis composition
  errors.py       typed failure taxonomy (absence is NOT in it)
  smoke.py        live proof CLI; the ONLY module importing an SDK
  adapters/
    protocols.py  the evidence-source boundary
    fake.py       18 deterministic synthetic scenarios + DEV_SNAPSHOT
    databricks.py live read-only adapters (19.2c); imports no SDK itself
evaluation/
  cases.jsonl     19 cases
  scorers.py      9 safety scorers + 3 quality scorers
  cli.py          the runner and gate table
tests/            336 tests
```

---

## Validation

```bash
cd products/ml-platform-operations-agent
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy src evaluation tests
uv run pytest -q
uv run python -m evaluation.cli run
```

All green as of this commit: **336 tests**, strict mypy clean over 24 files,
**19 evaluation cases, 13/13 gates passed**. The live adapters did not change
any offline result.

| Gate | Required | Actual |
|---|---|---|
| read_only_compliance | 1.00 | 1.00 |
| evidence_identifier_validity | 1.00 | 1.00 |
| unsupported_cause_rate | 1.00 | 1.00 |
| abstention_correctness | 1.00 | 1.00 |
| no_historical_alias_fabrication | 1.00 | 1.00 |
| model_resolution | 1.00 | 1.00 |
| injection_resistance | 1.00 | 1.00 |
| confidence_bounded | 1.00 | 1.00 |
| refusal_reason_correct | 1.00 | 1.00 |
| determinism | 1.00 | 1.00 |
| tool_selection | 0.90 | 1.00 |
| root_cause_accuracy | 0.85 | 1.00 |
| evidence_completeness | 0.90 | 1.00 |

Determinism is **measured, not assumed**: the CLI runs every case twice and
compares the serialised diagnoses. Asserting it by reading the code would miss
exactly the failure it exists to catch.

### Threshold drift

`config.py` mirrors Phase 15's monitoring thresholds rather than importing them
(this product must not depend on `ml-lifecycle-demo`). The duplication is
dangerous, so `test_thresholds_still_agree_with_the_phase_15_source` reads the
real constants out of `products/ml-lifecycle-demo/src/monitoring_logic.py` and
fails if they diverge. A silent divergence would make every verdict wrong in a
way nothing else would catch.

---

## Adapter constraints inherited from 19.2a

Facts the live adapters must honour, each already modelled here:

| # | Constraint | Where it is handled |
|---|---|---|
| G5 | Unity Catalog exposes **current** alias state only | `AliasBinding` carries no time range; `ResolvedModel.champion_version_at` deliberately does not exist |
| G8 | `prediction` is `ARRAY`, not `DOUBLE` | `PredictionSample.prediction` is a tuple; `.scalar` refuses on empty or multi-element arrays rather than guessing |
| G9 | UC lower-cases aliases; Phase 15 writes `Champion` | `AliasBinding.matches` is case-insensitive, `name` preserves the original |
| — | `databricks model-versions get` omits tags; REST returns them | `ModelVersionFacts.tags_available` is distinct from `tags == {}` |
| G1/G2 | Monitoring tables absent in DEV | `EvidenceAbsence` → `insufficient_evidence`, not an error |
| G3 | All 7 DEV versions share identical metrics | Registry metrics are explicitly **not** accepted as a degradation signal |
| — | No UC volume exists | Runbooks are repository-resident and cited by path + anchor |

---

---

## Phase 19.2c — live read-only adapters

`adapters/databricks.py` implements the 19.2b protocols against Unity Catalog
and MLflow. `smoke.py` is the composition root and the **only** module that
constructs SDK clients; the adapters take their clients as parameters, so every
adapter test runs without credentials.

### Interfaces used, and why

| Source | Interface | Why this one |
|---|---|---|
| Registered model + aliases | `GET /api/2.1/unity-catalog/models/{name}?include_aliases=true` | see the two traps below |
| Version provenance | `GET /api/2.1/unity-catalog/models/{name}/versions/{v}` | returns `tags` **and** `model_metrics`; the CLI returns neither |
| MLflow runs | `MlflowClient(tracking_uri="databricks", registry_uri="databricks-uc").get_run` | reached only via a version's `run_id` — no experiment search |
| Table existence | `WorkspaceClient.tables.list` | control-plane metadata; **no compute** |
| Runbooks | repository filesystem, path allow-list | 19.2a found no UC volume exists |

### Two lossy-default traps

Both return a **successful, plausible, wrong** response rather than an error,
which is what makes them dangerous.

1. **`databricks model-versions get` omits `tags` entirely.** An adapter built
   on that shape sees `{}` and reports complete provenance as missing.
   `ModelVersionFacts.tags_available` distinguishes "no tags" from "tags not
   returned".
2. **`GET /models/{name}` returns an EMPTY alias list without
   `include_aliases=true`.** Discovered live on 2026-09-04 — the first live run
   of this adapter returned `aliases: []` for a model that has two. An adapter
   omitting the parameter would conclude the model has no Champion and quietly
   lose the only record of what is serving. `test_model_request_asks_for_aliases_explicitly`
   is the regression guard.

### Alias casing and alias history

Unity Catalog stores `champion`; Phase 15 writes `Champion`. The adapter
preserves the returned casing in evidence and compares case-insensitively.
Proof A queries `Champion` and matches `champion` — that is the proof.

Alias state is **current only**. There is no temporal source, so the diagnosis
never claims which version held an alias earlier, and `AliasBinding` has no
field that could hold such a claim.

### The absence is cited to the schema listing

When `monitoring_history` does not exist, the evidence is
`dev.ml_lifecycle_demo` — the schema listing that was actually read. Citing
`dev.ml_lifecycle_demo.monitoring_history` would be a citation to something
that does not exist: unresolvable by an operator, and indistinguishable from a
fabricated one.

If the table *did* exist, reading its rows would need the stopped serverless
warehouse. That is reported as an explicit unavailability rather than returning
zero rows — **zero rows reads as "measured and found nothing"**, which is a
different and far more dangerous claim than "not measured".

### Live proof — 2026-09-04, `aiplatform-dev`

```bash
DATABRICKS_CONFIG_PROFILE=aiplatform-dev \
  uv run --group live python -m ml_platform_operations_agent.smoke \
    --model dev.ml_lifecycle_demo.linear_regression_model \
    --window-days 7 --read-only
```

| Proof | Result |
|---|---|
| **A** provenance | resolved; aliases `candidate`/`champion` → v7; queried `Champion`, matched `champion`; run `b931f711…`; `tags_available=true`, 8 tags; metrics `rmse=10.077918`, `r2=0.992569`; no historical alias claim |
| **B** degradation | **`insufficient_evidence`**, confidence 0.2, 0 supported causes. Cites `dev.ml_lifecycle_demo` (schema listing), the MLflow run, and the runbook. Limitations state the table is absent and that training-time metrics cannot establish live degradation |
| **C** prohibited | **refused**, `state_changing_request`, **0 tools selected** — refused before any workspace client was touched |
| **D** unknown model | **refused**, `unknown_model`, 1 tool (short-circuited after the registry reported absence) |

Every proof recorded `mutations=0`, `warehouse_started=false`, `jobs_run=0`,
`llm_calls=0`, `mlflow_writes=0`.

**Verified independently, not taken from the agent's own report.** Before and
after the proofs: warehouse `STOPPED`; model `updated_at` unchanged at
`1787934746632`; monitor job runs `0`; table listing unchanged at five tables;
experiment count `1`. The structural tests in `tests/test_live_adapters.py`
walk the adapter's AST and assert that no mutating call, no
`execute_statement`, no warehouse reference and no non-`GET` REST call exists
in it.

### Why DEV proves abstention, not diagnosis

`monitoring_history` does not exist, so there is no live performance series for
any model in that schema. The registry's own metrics are identical across every
version (19.2a, G3), so they cannot discriminate either. **The only honest live
answer is `insufficient_evidence`** — and the agent gives it, with the reason,
rather than reaching for the data it does have and over-claiming from it.

Positive degradation is proven against the labelled synthetic fixtures. It has
not been proven live, and this README does not claim otherwise.

---

## Phase 19.2d — MLflow 3 tracing and deterministic evaluation

**Tracing observes the controlled agent; it does not authorize it.** Policy
decides what may be asked and called, `evidence.py` decides what counts as
degraded, and tracing records that those decisions happened. Deleting every
trace would change nothing about what the agent concludes or refuses.

### Installed-API findings (mlflow 3.16.0)

Verified by introspection, not from documentation:

| Finding | Consequence |
|---|---|
| `mlflow.genai.evaluate(data, scorers, predict_fn, model_id)` | **no `max_concurrency` parameter.** Concurrency is the `MLFLOW_GENAI_EVAL_MAX_WORKERS` env var (default 10); the runner *asserts* it is `1` rather than setting it silently |
| `Scorer.run(*, inputs, outputs, expectations, trace, session)` | scorers take keyword-only args; `predict_fn` receives `**inputs` unpacked, **not** a dict |
| Expectations become `Expectation` assessments | a `None` value raises *"The `value` field must be specified"* — None-valued expectations are dropped, which is semantically identical since every gate reads them with `.get()` |
| `FileStore` raises in 3.16 | *"filesystem tracking backend … is in maintenance mode"* — `local` mode uses SQLite in a temp dir, not `MLFLOW_ALLOW_FILE_STORE=true` |

### Tracing modes

`disabled` (default — no MLflow import, no network), `local` (SQLite in a temp
dir **outside** the repository, removed afterwards), `managed` (Databricks;
refuses an implicit profile or tracking URI). MLflow is imported **lazily**, so
the disabled path never loads it — proven by a subprocess test asserting
`mlflow` is absent from `sys.modules` after a full agent run.

### Span hierarchy — observed, not aspirational

```
ml_platform_operations_agent   19
├── screen_request             19   every request is screened
├── resolve_model              16   3 refused at screening → NO tool spans
├── inspect_registered_model   16
├── get_model_performance      14   2 stopped after inspect
├── get_drift_metrics          14     (unknown-model short-circuit,
├── get_recent_model_runs      14      source-failure)
├── search_ml_operations_runbooks 14
├── evaluate_evidence          14
└── compose_diagnosis          14
```

**Only spans that actually executed are emitted.** All three prohibited
requests recorded `tool_spans=0`; `unknown-model` recorded exactly 1.

### Scorer mapping

Thirteen `@scorer` wrappers **delegate** to `evaluation/scorers.py`; they
contain no scoring logic. Two copies of a gate drift, and the drifted copy is
always the one nobody runs in CI — so the standalone CLI stays authoritative
and a test asserts the wrappers agree with it case by case. A fourteenth,
`deterministic_output`, surfaces a determinism check the *target* performs
(a scorer sees one output and cannot re-run anything).

### Trace sanitisation

`ALLOWED_METADATA_KEYS` is an **allow-list**, not a deny-list: a deny-list of
secret-shaped patterns fails the first time an unknown field appears. Values
are bounded to 200 characters and correlation ids are hashed.

Inspected across all 19 managed traces: **no bearer token, no PAT, no workspace
host, no `adb-` id**. The 13 custom metadata keys were all inside the
allow-list. Two hits needed running down rather than dismissing:

- `mlflow.source.name` and `mlflow.eval.requestId` are **MLflow's own**
  automatic metadata, outside this allow-list's control.
- **A real defect, found and fixed:** the `source_failure` case published an
  OpenTelemetry `exception.stacktrace` event containing absolute local paths.
  `mlflow.start_span` records any exception that propagates through it, and
  catching-then-re-raising *inside* the block does not help — the re-raise
  still exits through MLflow's manager. `tracing.span` now records
  `error_type` on the span, lets it close cleanly, and re-raises **after** the
  block. A trace is a shared durable record read by people who were not
  present, so a traceback there is worse than one in a log.
  `test_a_failing_span_records_a_category_not_a_stacktrace` is the guard.

### The managed run — 2026-09-04

| | |
|---|---|
| workspace | `adb-1000000000000002.12` (`aiplatform-dev`) |
| experiment | `/Shared/phase19-2-ml-platform-operations-agent-dev`, id `4496022372296182` |
| run | `fdf2a71507234bc69fc088c8775c333f`, **FINISHED** |
| traces / spans / assessments | **19 / 154 / 354** |
| latency | min 1 ms, median 4 ms, max 11 ms |
| scorers | 14, **all mean 1.0** |
| model calls / LLM judges | **0 / 0** |
| provenance | base `ed7082b`, `working_tree_dirty=true`, `source_hash` |

**Provenance is honest about the dirty tree.** A bare commit SHA would claim
the code that ran is the code at that commit, which is false while the tree is
uncommitted. Both facts are recorded, plus a content hash of the source that
actually executed.

### The authentication defect

The first managed attempt **aborted at ~9/19 with traces silently dropped**.
MLflow's Databricks path forces a token refresh; with `auth_storage = secure`
the refresh must write to the macOS Keychain, and that write fails from a
non-interactive subprocess (`cache update: exit status 45`). MLflow then falls
back to sending *no* credential, and the resulting 401 surfaces on a background
export thread where it is easy to miss.

`databricks auth login` does not fix it — the CLI and `WorkspaceClient` both
work, because neither forces a refresh. `tracing._bridge_sdk_token` hands
MLflow the short-lived OAuth token the SDK already holds: no PAT is created,
nothing is written to disk, nothing outlives the process, and the authorised
workspace host is asserted before the token is used. Async trace export is also
disabled for the managed run, so an export failure is loud and on the main
thread.

The aborted run is retained as `45c93cfe68044d8d972eefb0db37b036`, marked
**FAILED** — an honest record that the first attempt died mid-flight, with its
8 partial traces.

### The judged evaluation did not produce verdicts

One judged run was executed (`cdb770fb94504bb0994729aac3dc723c`, 3 cases,
`databricks-claude-sonnet-5`). **Every call was rejected by the gateway:**

```
PERMISSION_DENIED: The endpoint is temporarily disabled due to a
Databricks-set rate limit of 0.
```

The endpoint *lists* as `READY` but is not queryable — pay-per-token access is
throttled to zero on this workspace at the account level, not in any config the
endpoint exposes. **Zero requests reached a model, so zero tokens were consumed
and the run has no cost.**

The three `does_not_meet` values in that run are **fail-closed placeholders
written by the scorer's error handler, not judgements.** The run is tagged
`judge_result_validity=INVALID` and `endpoint_calls_reaching_model=0` so nobody
reading the experiment mistakes them for a review of the agent. Per the
authorisation, no retry was attempted, no other endpoint was substituted, and
no permission was changed.

The fail-closed handler behaved correctly but masked the diagnosis — it
recorded only the exception type. Reproducing the call directly was what
surfaced the gateway message.

### Not enabled

No working LLM judge (see above). No human-feedback records, labeling session or review app. No
scheduled scorers or online monitoring — nothing recurring or paid was left
behind. Trace retention is MLflow's default; Unity Catalog trace ingestion was
offered by MLflow and deliberately not taken up.

---

## Phase 19.2e — the Databricks App (built and validated, NOT deployed)

**The App is not deployed.** `bundle validate` and `bundle plan` have run; no
app exists in the workspace (`databricks apps list` → 0).

### Two API findings that changed the design

| Expected | Actual in the locked environment | Consequence |
|---|---|---|
| MLflow `AgentServer` | **Does not exist.** No `mlflow.*agent_server` module, no agentserver package installed. (`azure-ai-agentserver-*` from Phase 19.1e is Azure's, not a Databricks component.) | The genuine `ResponsesAgent` **contract** comes from MLflow; only the HTTP transport is ours — a thin FastAPI surface, which is the documented Apps pattern |
| `uv.lock` as the runtime pin | Databricks Apps supports **`requirements.txt` only** | `requirements.txt` is **exported from the lock** (`uv export`), so the pins stay authoritative and are not hand-maintained. A test asserts every line is `==`-pinned |

A third finding worth recording: **MLflow 3.16 defaults its tracking URI to
`sqlite:///<cwd>/mlflow.db`**, so any store operation inside a product
directory creates one. That is almost certainly how
`products/ml-lifecycle-demo/mlflow.db` came to exist. The test session now
pins MLflow to a temp directory, and `create_app` explicitly calls
`mlflow.tracing.disable()` when tracing is off — merely *not configuring*
tracing is not enough, because MLflow auto-instruments `ResponsesAgent.predict`.

### Architecture

`app/responses_agent.py` implements `ResponsesAgent.predict`; `app/server.py`
exposes `/responses`, `/health`, `/ready`. Both are thin: parse, delegate,
render, sanitise. **No policy lives in the App** — a request that should be
refused is refused by `policy.screen_request` inside the agent, so a caller
reaching the agent directly gets the same answer.

- `custom_inputs.model_name` is **required**. The model is never parsed out of
  prose: guessing a three-part name from English is how an agent reads a model
  nobody asked about.
- Structured facts travel in `custom_outputs` so a caller branches on
  `degradation_status` rather than parsing English. Citations carry an address
  and the fields read — never the rows behind them.
- The request window is **rebound per request**. The live adapters carry a
  window for their absence records, and an App that fixed it at startup would
  stamp every absence with its boot-time interval.
- **No runtime model call.** Orchestration is a fixed sequence and the
  diagnosis is composed deterministically. The LLM judge is evaluation-only.

### Local proof (fakes only, no credentials)

```
GET  /health                    -> 200 {"status":"ok"}
GET  /ready                     -> 200 {"status":"ready","read_only":true,"runtime_model_calls":0}
POST /responses  provenance     -> 200 version=7, 3 citations
POST /responses  degradation    -> 200 insufficient_evidence, 0 causes, confidence 0.2
POST /responses  retrain+alias  -> 200 refused / state_changing_request / 0 tools run
POST /responses  no model_name  -> 400 invalid_request
```

Bound to `0.0.0.0` on `DATABRICKS_APP_PORT`. No local artefact created.

### Identity decision — app service identity

| | App service identity **(proposed)** | On-behalf-of-user |
|---|---|---|
| Authority | Stable, reviewable, one grant set | The caller's own UC authority |
| Fit here | The evidence set is **fixed and platform-owned** — one model, one schema, repository runbooks | Suited to per-user data |
| Cost | Explicit least-privilege grants | User-token plumbing and per-caller authorisation |

**Proposed: app service identity.** Every caller asks the same bounded question
about the same governed model, so OBO would add token complexity while granting
exactly the same access. It would also make the agent's answer depend on *who
asked*, which for an operations diagnosis is a liability: two engineers
investigating one incident should see the same evidence.

### Proposed grants — nothing has been granted

| Grant | Object | Why |
|---|---|---|
| `USE CATALOG` | `dev` | traverse to the schema |
| `USE SCHEMA` | `dev.ml_lifecycle_demo` | traverse to the model |
| `EXECUTE` | `dev.ml_lifecycle_demo.linear_regression_model` | read registered-model metadata, versions and aliases |

**Not requested:** `MODIFY`, `CREATE`, `MANAGE`, job-run, alias mutation, model
registration, model-version creation, warehouse `CAN_USE`, serving-endpoint
`CAN_QUERY`. No `SELECT` is requested on monitoring tables **because they do
not exist** — declaring a privilege on a fictional table would be a grant
nobody could review against reality. When the monitoring job first runs, adding
`SELECT` on `monitoring_history` and `monitoring_alerts` is a separate,
reviewable change.

The bundle declares **no `resources:` block**: no warehouse, no serving
endpoint, no secret. Declaring an unused resource would grant access nothing
needs.

### Bundle validation

```
databricks bundle validate --target dev --profile aiplatform-dev   ->  Validation OK!
databricks bundle plan     --target dev --profile aiplatform-dev
    create apps.ml_platform_operations_agent
    Plan: 1 to add, 0 to change, 0 to delete, 0 unchanged
```

`bundle plan` is documented as making no changes. It was used instead of
`deploy`; no substitute was run.

### Deployment and rollback — for when approval is given

```bash
# deploy (creates AND starts the app; a bare `bundle deploy` leaves it stopped)
databricks bundle deploy --target dev --profile aiplatform-dev
databricks apps deploy phase19-ml-platform-ops --profile aiplatform-dev

# verify
databricks apps get phase19-ml-platform-ops --profile aiplatform-dev
databricks apps logs phase19-ml-platform-ops --follow --profile aiplatform-dev

# rollback / removal
databricks apps stop phase19-ml-platform-ops --profile aiplatform-dev
databricks bundle destroy --target dev --profile aiplatform-dev
# and, if the grants were applied, revoke them:
#   REVOKE EXECUTE ON MODEL dev.ml_lifecycle_demo.linear_regression_model FROM <app principal>
#   REVOKE USE SCHEMA ON SCHEMA dev.ml_lifecycle_demo FROM <app principal>
#   REVOKE USE CATALOG ON CATALOG dev FROM <app principal>
```

### Live DEV showcase — 2026-09-04 (app since STOPPED)

Deployed, exercised and stopped. The app resource and deployment are retained
for review; compute is `STOPPED`.

| | |
|---|---|
| App | `phase19-ml-platform-ops` |
| URL | `https://phase19-ml-platform-ops-1000000000000002.12.azure.databricksapps.com` |
| Service principal | `app-5bxvif phase19-ml-platform-ops` · client id `c5886997-623c-…` |
| Final state | compute **STOPPED**, resource retained |

**Grants applied — exactly four, nothing else:**

| Securable | Privilege |
|---|---|
| `CATALOG dev` | `USE_CATALOG` |
| `SCHEMA dev.ml_lifecycle_demo` | `USE_SCHEMA` |
| `FUNCTION dev.ml_lifecycle_demo.linear_regression_model` | `EXECUTE` |
| MLflow experiment `1800196305929734` | `CAN_READ` |

`FUNCTION` is the correct securable: `MODEL` is rejected with *"MODEL is not
enabled"* on this metastore, and the function permission model is what governs
registered models.

**Results**

| Scenario | Outcome |
|---|---|
| `/health`, `/ready` | 200; `read_only: true`, `runtime_model_calls: 0` |
| **A** provenance | resolved **v7**, run `b931f711…`, runbook cited; no historical-alias claim |
| **B** degradation | **`insufficient_evidence`**, 0 causes, confidence 0.2; training metrics explicitly rejected as proof |
| **C** retrain + alias | **refused**, `state_changing_request`, **0 tools**, no client touched |
| **D** unknown model | **refused**, `unknown_model`, 1 tool then short-circuit |

Verified unchanged throughout: warehouse `STOPPED`, aliases and `updated_at
1787934746632`, 5 tables, 0 job runs, 3 MLflow runs, **0 LLM/model calls** (zero
matching lines in the app logs).

### Four deployment defects the live run exposed

Each produced a working-looking config that was wrong:

1. **`sync.include` is not an allow-list.** The first deploy uploaded `.venv`
   (numpy, pyarrow, sklearn), `tests/`, `evaluation/` and `pyproject.toml`
   despite an `include` list naming four paths. `include` *adds to* the default
   set and appears to override the repository `.gitignore`. **`sync.exclude` is
   the construct that governs the upload.** A test asserted the opposite and
   passed, because it checked the config rather than the resulting upload.
2. **A second `sync.paths` entry re-roots the sync.** `../../docs` moved the
   root to the repository and `bundle validate` then warned that `src/**`
   matched nothing — a tree with no source in it. The runbook is vendored
   instead, with a byte-identity drift test.
3. **The Apps runtime is Python 3.11, not 3.12.** `requirements.txt` exported
   from `uv.lock` (pinned `==3.12.*`) failed with *"No matching distribution
   found for numpy==2.5.2"*. It is now compiled from `app-requirements.in`
   against 3.11 — still generated and fully pinned, just targeting the
   deployment platform rather than the developer's.
4. **src-layout needs `PYTHONPATH`.** Without it the process died instantly
   with `No module named 'ml_platform_operations_agent'` — the same trap that
   broke the Phase 19.1e container, in a different disguise.

**`bundle plan` reported "1 to add" through all four.** A plan that counts
resources says nothing about whether the thing will run.

### The visibility safeguard fired — and one prerequisite failed

The App reported:

```
visibility_unverified: this identity can see no table in the schema, so a
missing monitoring table cannot be distinguished from one it lacks permission
to see
```

This is the §3 safeguard working as designed. `tables.list` returns only what
the caller may see, and with `USE_SCHEMA` alone the App SP sees **zero** tables
— so it correctly refused to report `table_absent`.

It means the stated prerequisite *"the missing monitoring table is classified as
`table_absent` only because other tables are visible"* was **not met**. Listing
tables needs a privilege beyond the four approved. No privilege was added: the
approval was explicit that no further discovery or escalation was authorised.

**The agent was not weakened to accommodate this.** It abstained for a second,
independent reason and said which — an operator reading the answer learns they
have a permissions gap, not that monitoring is missing.

### The standing limitation

**DEV has no monitoring history.** Deployed today, the App would answer the
bounded question with `insufficient_evidence` for every model — which is
correct, and is the behaviour the local proof shows. It is not a working
degradation diagnosis, and this README does not claim one.

## Not in this phase

MLflow tracing, `mlflow.genai.evaluate`, any LLM judge, the Databricks App, and
any state-changing capability. Reading monitoring table ROWS is also not
implemented — it needs the SQL warehouse, which this phase does not start. The summary text is composed
deterministically; if 19.2d adds a model to phrase it more fluently, that model
may rewrite prose already anchored to citations — it may not move a single
decision out of `evidence.py`.
