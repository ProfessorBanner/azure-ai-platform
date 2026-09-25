# Foundry capability lab (Phase 16.2)

A data-plane capability proof for the sandbox Microsoft Foundry deployment
provisioned in Phase 16.1.

It answers one question: **can this platform call its Foundry deployment
keylessly, with a Microsoft Entra ID token, and get back a strictly typed,
schema-validated result with usable telemetry?** Everything here exists to
establish that, and to make each way it can fail distinguishable.

> **This is not the Phase 17 application.** Phase 17 will build
> `products/enterprise-llm-demo` as a provider-neutral FastAPI/RAG product with
> its own `FoundryLLMProvider` adapter. This lab is a bounded experiment that
> may be deleted once Phase 17 owns the capability. Do not import it from a
> product, and do not grow it into one. There is no FastAPI here, no retrieval,
> no vector store, no agent and no tool calling — those are Phases 17 and 18.

## Why `labs/` rather than `products/`

`products/` holds deployed, promoted, pipeline-governed workloads. This is a
throwaway proof with no environment, no promotion path and no pipeline. Placing
it under `products/` would imply a lifecycle it does not have. It is otherwise
structured exactly like an existing product sub-project — its own
`pyproject.toml` and `uv.lock`, `src/` + `tests/` — because the lab needs the
OpenAI SDK, `azure-identity` and Pydantic, and the repository root project must
stay dependency-free.

## Two different endpoints

A Foundry resource publishes more than one address. They are not alternative
spellings of the same thing — they serve different APIs, and the environment
variable names below are chosen so the two cannot be muddled.

| | **Azure OpenAI endpoint (model data plane)** | Foundry project endpoint |
|---|---|---|
| Variable | **`AZURE_OPENAI_ENDPOINT`** | `FOUNDRY_PROJECT_ENDPOINT` *(reserved)* |
| Looks like | `https://<subdomain>.openai.azure.com/openai/v1/` | `https://<name>.services.ai.azure.com/api/projects/<project>` |
| API | OpenAI-compatible **Responses API** (`openai-responses-v1`) | project-scoped Foundry SDK operations |
| Used for | **running inference against a deployment** | evaluations, project resources, and later agents |
| Terraform output | `llm_endpoint` | `llm_project_endpoint`, `foundry_project_endpoints` |
| Used by this lab | **yes — exclusively** | no |

This lab is a **model data-plane** proof. It calls the Responses API at
`AZURE_OPENAI_ENDPOINT` and nothing else.

`FOUNDRY_PROJECT_ENDPOINT` is **reserved terminology**, declared in `config.py`
as `PROJECT_ENDPOINT_VAR` but deliberately never read here. Project-scoped work
— Foundry-native evaluation runs, tracing, and eventually agents — belongs to
Phase 16.3 and later, and will use that name. Naming it now means the project
endpoint has one agreed name across the repository before anything depends on it.

### Where to get the right value

The account publishes over sixty endpoints. Three matter, and the most obvious
one is wrong:

| Source | Value | Use it? |
|---|---|---|
| `properties.endpoint` | `https://<subdomain>.cognitiveservices.azure.com/` | **No.** Generic Cognitive Services account endpoint; does not serve the Responses API |
| `properties.endpoints["OpenAI Language Model Instance API"]` | `https://<subdomain>.openai.azure.com/` | Correct **host**, but only the host — still needs `/openai/v1/` |
| Terraform output **`llm_endpoint`** | `https://<subdomain>.openai.azure.com/openai/v1/` | **Yes.** The canonical, complete URL |

`AZURE_OPENAI_ENDPOINT` is validated at startup and must use HTTPS, must not be
the `cognitiveservices.azure.com` host, and must end with `/openai/v1/`
(trailing slash included). Validation happens **before any credential is
minted, any client is built or any call is made**, and a bad value is exit 2 —
turning what would otherwise be a confusing runtime 404 into a precise startup
message. A 404 at call time is likewise treated as structural configuration
failure, not as a transient error.

Setting `AZURE_OPENAI_ENDPOINT` to a project endpoint will fail for the same
reason: the Responses API is not served there.

## Why `DefaultAzureCredential`

The Foundry account is deployed with `local_auth_enabled = false`: API keys are
switched off at the service, so there is no key to use even if you wanted one.
Authentication is a Microsoft Entra ID bearer token.

`DefaultAzureCredential` is used because the same line of code resolves a
developer's `az login` session locally and a managed identity later, with no
branching and no secret in either case. That is the property that lets Phase 17
inherit this adapter shape unchanged.

The lab actively refuses key authentication: if `AZURE_OPENAI_API_KEY`,
`OPENAI_API_KEY` or any similar variable is set, configuration fails with a
`ConfigurationError` naming the variable (never its value) rather than quietly
changing how it authenticates.

### Token audience

The default audience is **`https://ai.azure.com/.default`**, chosen explicitly
for the Foundry `/openai/v1/` Responses API this lab calls.

An audience belongs to the **endpoint and API contract**, not to the caller or
to Azure generally: a token minted for one audience is simply not accepted by
another. That is why the value is stated rather than inferred.

`AZURE_OPENAI_AUTH_SCOPE` can override it, but purely as a **diagnostic** escape
hatch — for instance when deliberately probing a different API surface. The lab
does **not** try audiences in turn and has no fallback. A wrong audience must
surface as a clean `401` that names the cause; silently retrying with another
scope would hide a real misconfiguration and make the failure unlearnable.
Guessing between scopes is a debugging step a human takes deliberately, never
normal runtime behaviour.

## Required data-plane RBAC (conceptual — nothing is granted automatically)

A token proves *who you are*; it does not grant access. The signed-in principal
also needs a role that carries Foundry **data** actions.

| Role | Role definition ID | Scope |
|---|---|---|
| **`Foundry User`** | `53ca6127-db72-4b80-b1b0-d745d6d5456d` | the **Foundry account resource** |

This is a Microsoft Foundry resource with projects, not a classic
Azure-OpenAI-only account, so `Foundry User` is the correct role. Do not use
`Cognitive Services User` or `Cognitive Services OpenAI User` here.

### Owner and Contributor are not enough

This is the part that surprises people. Azure separates **control-plane**
permissions (`actions` — manage the resource) from **data-plane** permissions
(`dataActions` — call the resource). Owner and Contributor grant `actions: ["*"]`
but declare **no `dataActions` at all**. So a subscription Owner can create,
reconfigure and delete this Foundry account, read its diagnostics, and still be
refused when it tries to run a single inference call.

`Foundry User` is the role that carries `dataActions: ["Microsoft.CognitiveServices/*"]`,
which is what actually authorises the call this lab makes. An inherited
Owner/Contributor grant does not substitute for it, and adding one will not clear
a `category=authorization` failure.

Scope the assignment to the **Foundry account resource**, not the subscription
or resource group.

This repository does **not** create the assignment, and neither does this lab.
Every Terraform deployment identity holds `Contributor`, which cannot write role
assignments, and `az role assignment create` is an approval-gated command under
`CLAUDE.md`. See `docs/adr/0006-foundry-capability-lab.md` for why this is
deliberate and interim.

Until the role is granted, the lab fails with `category=authorization`.

## Local authentication

```bash
az login
az account show           # confirm the expected tenant and subscription
```

The account is also **default-deny at the network layer**. Your machine's public
egress address must be in the capability root's `allowed_ip_cidrs`. If it is
not, the lab fails with `category=network_denied`.

## Configuration

All non-secret. There is no secret to configure.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | yes | — | model data-plane base URL, ending `/openai/v1/` |
| `AZURE_OPENAI_DEPLOYMENT` | yes | — | deployment name to invoke |
| `AZURE_OPENAI_TIMEOUT_SECONDS` | no | `30` | per-request timeout |
| `AZURE_OPENAI_AUTH_SCOPE` | no | `https://ai.azure.com/.default` | Entra token audience (diagnostic override) |
| `FOUNDRY_PROJECT_ENDPOINT` | — | — | **reserved**; not read by this lab (see above) |

Never commit real endpoints, IP addresses, tenant ids, subscription ids or
tokens. Read the endpoint and deployment from Terraform outputs at run time.

## Running the deterministic tests

These never contact Azure. No credential, no network, no subscription. A
`conftest.py` fixture strips credential and endpoint variables from the
environment so the suite behaves identically on a developer laptop with an
active `az login` and on a clean agent.

```bash
cd labs/foundry-capability-lab
uv sync --all-groups

uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src tests
uv run pytest tests -v
```

## Running the one live smoke test

Explicitly manual. No pipeline runs this: hosted Azure DevOps agents cannot
reach the IP-restricted account, and adding a self-hosted agent is out of scope.

```bash
cd labs/foundry-capability-lab

export AZURE_OPENAI_ENDPOINT="https://<subdomain>.openai.azure.com/openai/v1/"
export AZURE_OPENAI_DEPLOYMENT="<deployment-name>"
# optional:
# export AZURE_OPENAI_TIMEOUT_SECONDS=30

uv run python -m foundry_capability_lab.smoke
```

Read the two required values straight out of Terraform rather than typing them:

```bash
cd infrastructure/capabilities/ai-foundry/sandbox
terraform output -raw llm_endpoint
terraform output -raw llm_deployment
```

The smoke test sends one fixed, synthetic observation. It is hard-coded rather
than read from the command line so a routine capability check cannot become an
accidental channel for real data.

## Expected output

On success (exit code `0`, stdout):

```
Foundry data-plane smoke test: SUCCESS
  ok deployment=gpt-4-1-mini latency_ms=612.4 total_tokens=168 request_id=resp_...
  model=gpt-4.1-mini
  api_contract=openai-responses-v1
  input_tokens=120
  output_tokens=48
  assessment:
    risk_classification=service_degradation
    severity=medium
    requires_escalation=False
    evidence_ids=['ALERT-4471', 'INC-2208']
    rationale_chars=88 (content not printed)
```

The prompt and the model's free-text `rationale` are deliberately **not**
printed — only their structure and length. The goal is to prove the call works,
not to display content. Telemetry is built from identifiers, counts, durations
and enums only, so the whole record is safe to log.

## Telling the failure modes apart

On failure the lab exits `1` and writes a typed category plus a remedy to
stderr. The distinction matters because each needs a different fix.

| Category | Cause | What to do |
|---|---|---|
| `configuration` | missing variable, or an API key is set | fix the variables; unset any key |
| `authentication` (401) | no/invalid Entra token, wrong audience | `az login`; check `AZURE_OPENAI_AUTH_SCOPE` |
| `authorization` (403) | token valid, **role missing** | grant `Foundry User` on the account (Owner/Contributor will not do) |
| `network_denied` (403) | **IP not on the allow-list** | add your egress address to `allowed_ip_cidrs`, re-apply |
| `rate_limited` (429) | capacity exceeded | retry, or raise `deployment_capacity` |
| `timeout` | no response in time | raise `AZURE_OPENAI_TIMEOUT_SECONDS`; check the path |
| `invalid_structured_output` | response failed the schema | re-run; tighten prompt or schema if persistent |
| `provider_error` | 5xx, transport, anything else | retry; quote the `request_id` |

**401 vs 403 is the single most useful distinction:** 401 means Azure did not
believe who you are, 403 means it believed you and said no.

**Both 403s are not the same.** A missing role assignment and an IP denial both
arrive as HTTP 403 with completely different remedies, so the adapter inspects
the response body to separate them into `authorization` and `network_denied`.
When in doubt, `network_denied` correlates with a change of network (VPN,
office, home); `authorization` does not.

Every failure carries a `request_id` when the service supplies one — quote it in
a support case, and use it to find the matching record in the sandbox Log
Analytics workspace.

## Evaluation (Phase 16.3A)

A repeatable, application-owned check on whether the deployment still does the
job — separate from the smoke test, which only proves the call works at all.

### The metamodel

Five ideas, in order:

1. **A fixed dataset.** `data/risk_evaluation_cases.jsonl` holds 12 labelled
   observations covering all six classifications, with escalation balanced 6/6
   so "always escalate" cannot score well. It is version-controlled and never
   generated: a moving dataset makes a moving metric, and comparing this week's
   score with last week's would mean nothing.
2. **Ground truth in the case, not in a judge.** Each case carries the expected
   classification, severity, escalation decision, and the exact evidence
   identifiers that genuinely appear in its text. Scoring needs no second model,
   so the evaluation cannot drift because a judge changed.
3. **Deterministic scoring.** The model is non-deterministic; the scoring is
   not. Given the same recorded outcomes, `metrics.py` always returns the same
   numbers. That is what makes a regression attributable.
4. **Repetition measures consistency, never masks failure.** Each case is
   attempted `--repetitions` times (default 3). **There are no retries
   anywhere.** Re-running failures until they pass would turn an error rate into
   a measure of persistence and silently change every denominator.
5. **Thresholds are reviewed, not tuned at runtime.**
   `data/evaluation_thresholds.json` is version-controlled, so lowering a bar is
   a diff someone approves.

### Metrics and their denominators

Two denominators, stated in every report:

| Denominator | Meaning |
|---|---|
| `attempts` | every call made — successes and failures alike |
| `valid_outputs` | attempts that returned a schema-valid assessment |

| Metric | Over | Gated | Meaning |
|---|---|---|---|
| `schema_validity_rate` | attempts | yes (= 1.0) | responses that satisfied the schema |
| `classification_accuracy` | valid_outputs | yes (≥ 0.75) | correct risk category |
| `escalation_accuracy` | valid_outputs | yes (≥ 0.75) | correct escalate/don't-escalate |
| `evidence_validity_rate` | valid_outputs | yes (≥ 0.9) | cited only evidence the case supports |
| `repeated_call_consistency` | repeated cases | yes (≥ 0.6) | cases whose repetitions all agree |
| `provider_error_rate` | attempts | yes (≤ 0.1) | attempts that failed |
| `severity_accuracy` | valid_outputs | no | most subjective field; observational |
| `latency_p50_ms` / `latency_p95_ms` | attempts | p95 only, no | nearest-rank, so always a real measurement |
| `input`/`output`/`total_tokens` | attempts | no | cost signal |

Accuracy divides by `valid_outputs` because a response that never validated has
no classification to be right or wrong about; dividing by `attempts` would
conflate "unreachable" with "incorrect". A case that failed on any repetition is
**not** counted as consistent, and cases attempted only once are excluded from
consistency entirely — one sample says nothing about repeatability.

### Pacing: why a live run needs `--inter-attempt-delay-seconds`

A deployment's capacity is a rate limit, and an unpaced evaluation measures the
throttle rather than the model. Measured on this deployment at **capacity 1**
(1 request and 1,000 tokens per 60 seconds, against roughly 547 tokens per
call), a 36-attempt run returned **1 valid output and 35 rate-limited
failures**.

Two changes address that:

- **Capacity raised 1 → 10** in the sandbox capability root, giving 10 requests
  and 10,000 tokens per 60 seconds. The request limit binds before the token
  limit, so pacing is a simple division.
- **`--inter-attempt-delay-seconds` is mandatory for `--provider foundry`** and
  must be greater than zero. It has no default for live runs on purpose: the
  right delay depends on the provisioned capacity, and baking a number into
  application code would hide a deployment-specific assumption that goes stale
  the moment capacity changes. Divide 60 by the requests-per-minute allowance
  and add a margin — at capacity 10 that is `60 / 10 = 6`, so use `7`.

Pacing sleeps **between** attempts only, never before the first or after the
last: N attempts cost exactly N−1 delays. It changes the spacing of calls and
nothing else — attempt counts, repetitions and every metric denominator are
identical paced or unpaced. **There are still no retries.** The delay is applied
outside the timed region, so it never inflates latency.

### Abort on structural failure

Some failures mean every remaining call will fail identically. On
`authentication`, `authorization`, `network_denied`, `configuration` (including
a 404) or `rate_limited`, the run **stops immediately**. Continuing would
collect nothing but duplicate errors, and in the rate-limited case would
actively prolong the throttle.

An aborted run is marked `operationally_complete: false` and still writes its
reports — the partial record is the diagnostic — but it is **never** a quality
verdict, and it exits 2 regardless of what the partial numbers say.

`invalid_structured_output`, `provider_error` (5xx) and `timeout` deliberately
do **not** abort: a malformed response or a transient 500 is exactly the
quality and reliability evidence this harness exists to collect, so the run
continues and reports it.

### Running it

```bash
cd labs/foundry-capability-lab
uv sync --all-groups

# Offline rehearsal — no Azure, no credential, no network.
uv run python -m foundry_capability_lab.evaluation.cli --provider offline

# Against the real deployment (needs az login, the Foundry User role,
# and an allow-listed egress address).
export AZURE_OPENAI_ENDPOINT="$(cd ../../infrastructure/capabilities/ai-foundry/sandbox \
  && terraform output -raw llm_endpoint)"
export AZURE_OPENAI_DEPLOYMENT="$(cd ../../infrastructure/capabilities/ai-foundry/sandbox \
  && terraform output -raw llm_deployment)"

# --inter-attempt-delay-seconds is REQUIRED and must be > 0.
# At capacity 10 (10 requests/60s), 60/10 = 6, so use 7 for margin.
uv run python -m foundry_capability_lab.evaluation.cli \
  --repetitions 3 \
  --inter-attempt-delay-seconds 7
```

A full live run is 12 cases x 3 repetitions = 36 attempts, so at 7 seconds it
takes roughly 35 x 7 = 245 seconds of pacing plus call time — a little over five
minutes. That is the cost of not being throttled.

Reports are written to `artifacts/` (git-ignored — they are run output):
`evaluation-report.json` and `evaluation-report.md`.

`--provider offline` runs fixed keyword rules instead of the model. It exercises
the harness — dataset, metrics, gates, reports — and says **nothing** about
gpt-4-1-mini. Every report names its provider so an offline run cannot be
mistaken for a capability result, and the real provider is the default so the
fake is always opt-in.

### Exit codes

| Code | Meaning | What to do |
|---|---|---|
| `0` | operationally complete **and** every gated threshold passed | nothing |
| `1` | operationally complete **but** a gated quality threshold failed | read the report — this is a **result** |
| `2` | configuration failure, **or** the run was operationally incomplete | fix the operational cause and re-run |

The 1/2 split is the point. Exit 1 means every planned attempt was made and the
model missed a bar. Exit 2 means no fair measurement was taken — invalid
configuration, or a run that aborted on a structural failure.

Schema-invalid responses are **not** operational failures: a malformed response
is quality evidence, so it lowers `schema_validity_rate` and produces exit 1.

Reports are written for a setup failure (nothing to report) but **are** written
for an aborted run, marked `operationally_complete: false`. Collapsing 1 and 2
would make a missing role assignment or an exhausted rate limit look identical
to a quality regression.

### What reports contain

Case ids, expected and actual decisions, evidence validity and fabricated-id
counts, failure categories, latency and token aggregates, and gate verdicts.

**Observation text and model rationale never appear.** This is structural, not
editorial: neither field exists on `CaseOutcome`, so there is no redaction step
a later edit could forget. Tests assert the guarantee against the real dataset.

## Architecture

Four boundaries, deliberately separated so Phase 17 can reuse the shape.

| Module | Role | Knows about Azure? |
|---|---|---|
| `domain.py` | the risk-assessment schema | **no** |
| `telemetry.py` | what is recorded about a call | **no** |
| `errors.py` | typed failure taxonomy | **no** |
| `config.py` | non-secret environment configuration | names only |
| `provider.py` | protocol + Foundry/OpenAI adapter | **yes — the only place** |
| `smoke.py` | manual entry point and rendering | no (delegates) |

`RiskAssessmentProvider` is the seam tests substitute a fake for.
`classify_exception` is a pure function from an exception to a typed error, so
the entire failure taxonomy is unit-testable with no network, credential or
mocked HTTP stack. Nothing below `smoke.py` logs anything — the adapter returns
telemetry and raises typed errors, and the decision to print belongs to the
entry point. That is what keeps prompts and response bodies out of logs by
construction rather than by remembering to redact.

The Azure SDKs are imported lazily inside `from_config`, so the package (and the
whole test suite) imports without them present.
