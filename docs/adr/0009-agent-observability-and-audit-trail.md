# ADR 0009: Agent observability and audit trail

## Status

Accepted. Introduced by Phase 18.5 for `products/platform-engineering-assistant`.

Extends ADR 0008, which built the controlled agent. Not added to the assistant's
own corpus, for the reason ADR 0008 gives.

## Context

Phases 18.1–18.4 built a controlled agent: a model that proposes, a
deterministic policy layer that authorises, a registry that owns risk, an
executor with timeouts and idempotency, human approval bound to an argument
fingerprint, and a durable workflow that can be interrupted and resumed.

Each of those controls was demonstrated by unit tests against the code. None of
them was demonstrable **from a recorded run**. `AgentTelemetry` summarises a turn
in one row, which answers *what happened* but not *in what order, and what was
true at each step*.

That gap matters more than it looks. The two properties the whole design rests
on are statements about SEQUENCE:

- nothing reaches a tool without passing the policy layer;
- no state-changing tool runs before a human approved *these exact arguments*.

A property demonstrable only by reading the implementation cannot be checked per
run, cannot be evaluated per case, and is therefore one nobody notices losing.
Once a turn can be interrupted by an approval and resumed by a *different
process* — Phase 18.4 — reconstructing the order from a single summary row stops
being possible at all.

## Decision

### The trajectory is the record, and ordering is the point

One `AgentTrajectory` per turn: a header carrying provenance (prompt versions,
prompt hash, retrieval config, corpus version, provider, model, deployment) and
an ordered tuple of `TrajectoryEvent`s covering the whole path:

```
request_received -> model_decision -> policy_verdict -> approval_requested
                 -> approval_decided -> tool_call -> tool_result
                 -> state_transition -> outcome
```

Sequence numbers and elapsed times are assigned by the recorder, never by the
instrumented call site, so an instrumented path cannot get ordering wrong even
by accident. A caller supplies what happened; never where it belongs.

`AgentTrajectory.verify()` checks the invariants against the record: contiguous
sequence, monotonic time, a request first, exactly one outcome, no consequential
event after it, every call paired with a result, the iteration ceiling, and the
two ordering statements above. It is pure — no clock, no I/O, no model — which
is what makes it usable as the Phase 18.6 trajectory-correctness metric rather
than only as a unit-test assertion.

### The call is recorded before it runs

A trail that records only calls that *returned* cannot show an action that
started and never came back, which is exactly what a timeout or a crash leaves
behind. So `tool_call` is written first, with the id the executor will use —
`ToolExecutor.execute` now accepts a caller-supplied `tool_call_id` — and
`tool_result` follows with the same id. One id spans the audit trail and the
execution outcome.

### Redaction is structural, as everywhere else in this product

There is no field for the question, the answer, a prompt, tool arguments,
retrieved evidence or any reasoning. Not "stripped before writing" — the fields
do not exist, so there is no step to forget. Three fields look like content and
are not:

- `argument_fingerprint` — the Phase 18.2 idempotency hash over the *validated*
  arguments. It correlates "the same action" across proposal, approval and
  execution without carrying what the action says.
- `approver` — the deciding principal. The one identity field, present because
  accountability is the entire point of an approval record.
- `claimed_risk_level` — the model's assertion, which policy ignores. Recorded
  so a systematic attempt to misclassify tools is visible across turns.

The redaction is enforced twice: structurally, by tests over the declared model
fields, and behaviourally, by tests that run a turn with a marker string in the
question and in a tool argument and then search the serialised record for it.

### Observability may never break the product

A sink that raises is swallowed and counted; `CompositeTrajectorySink` isolates
each sink so one cannot silence the others. A misconfigured or unavailable sink
is dropped at startup, named in `TrajectoryObservability.degraded`, and logged —
the product still serves.

This is the one place in the product where configuration does **not** fail
closed, and the asymmetry is deliberate. For retrieval or credentials,
continuing would mean answering without a guarantee. Here, continuing means
answering without a log line. An agent that refuses to serve because its
telemetry backend is down has converted an observability outage into an
availability one.

### Sinks are operational configuration, and only sinks are

`AGENT_TRAJECTORY_SINKS` selects from `none` (default), `memory`, `jsonl`, `log`
and `export`. Environment-driven, unlike retrieval parameters and evaluation
thresholds, because *where a log line is written* is a property of one
deployment — and encoding it in the repository would mean a commit to change a
log destination. No setting can change what is recorded or what is redacted;
that is structural and lives in `agent/trajectory.py`.

`GET /v1/trajectories/{id}` serves the bounded in-memory sink plus the
`verify()` verdict. It is an inspection aid, not retention: a 404 is not
evidence that a turn did not happen.

## LangSmith: assessed and NOT adopted as a dependency

Phase 18.4 rejected LangGraph partly *because* it drags in LangSmith. This phase
assessed LangSmith on its own merits, as asked.

What it is good at is real: a run-tree waterfall, filtering across runs, dataset
capture from production traces, and sharing a trajectory with someone who was
not there. For a multi-step agent whose problems are "which step went wrong",
that is the right tool.

It is not adopted here, for three reasons that compound:

1. **We cannot give it the thing that makes it valuable.** LangSmith's interface
   is built around inputs and outputs — prompts, tool arguments, model text.
   This product does not COLLECT any of those. What it can export is
   identifiers, enums, counts, durations and hashes. A backend receiving only
   that is a span viewer, and we already have one in the platform.
2. **It needs a long-lived API key.** `LANGSMITH_API_KEY` is a bearer credential
   with no managed-identity path. The platform rules say plainly: use managed
   identities or workload identity federation, and do not create long-lived
   cloud credentials. Adopting LangSmith means breaking that rule for a span
   viewer.
3. **It is third-party egress.** Even metadata-only export sends this platform's
   agent behaviour — tool names, denial reasons, approval principals, failure
   patterns — to a vendor. That is a data decision, and a data decision should
   not arrive as a line in `pyproject.toml`.

**What was built instead is the seam.** `SpanExporter` is a one-method protocol;
`ExportingTrajectorySink` maps events onto it; `load_exporter` resolves a
`module:factory` path at runtime. A LangSmith adapter is roughly ten lines
written against that protocol, enabled with two environment variables, and
requires no change to the product. Nothing in the product — including the
adapter seam — imports a vendor package, and a hygiene test keeps it that way.

The integration is therefore bounded and *provable offline*: the CI suite
exercises the seam with a fake exporter, which is the only kind of integration
test a product whose CI has no network can honestly run.

Revisit when there is agent trajectory *evaluation* to do across many runs, and
when a data-egress decision has been taken deliberately rather than implied.

## Azure AI Foundry tracing: complementary, and the preferred future adoption

Foundry tracing is OpenTelemetry GenAI semantic conventions exported to
Application Insights / Azure Monitor. It is genuinely complementary rather than
duplicative, and it wins on every axis where LangSmith loses:

- it stays inside the Azure trust boundary, so it is not third-party egress;
- it authenticates with managed identity or workload identity federation, so it
  needs no long-lived key;
- it correlates the agent's spans with the Log Analytics workspace the platform
  foundation *already deploys*, and with the provider-call leg — model,
  deployment, latency, tokens — which is the one part of the picture the native
  trail records only as totals.

It is not adopted in this phase for one reason: it requires an Application
Insights resource, and Phase 18 forbids new infrastructure. That is a scheduling
constraint, not a rejection.

**So the duplication was avoided in advance.** `TrajectoryEvent.as_otel_attributes()`
maps every event onto the published GenAI convention names — `gen_ai.tool.name`,
`gen_ai.tool.call.id`, `gen_ai.usage.*` — with this product's own attributes
namespaced under `agent.`. Adopting Foundry tracing is then a sink
implementation against the existing seam, not a re-instrumentation. The mapping
lives next to the fields it maps, so there is one definition of what each field
means rather than one per exporter.

Mapping onto a convention rather than a vendor schema is the deliberate choice:
Foundry tracing *is* those conventions, LangSmith ingests them, and a convention
outlives whichever backend happens to be receiving it.

## Consequences

### Positive

- The two ordering properties the agent design rests on are now checkable
  against a recorded run, offline, with no model — and are therefore evaluable
  per case in Phase 18.6.
- A turn that was interrupted and resumed has a coherent audit trail: the start
  keeps one trajectory with its state transitions appended, and the resumed
  execution is its own trajectory linked by `workflow_id`.
- A human approval reaches the trail whichever caller recorded it, because
  `ApprovalService.decide` emits the event rather than the HTTP route.
- Phase 18.5 adds no new runtime dependency. The product's dependency set is
  still five packages.
- Adopting either candidate backend later is a config change plus an adapter,
  because the export mapping is a convention rather than a vendor schema.

### Negative / accepted

- The exported record is metadata-only, which limits what any backend can show.
  Debugging *why* a model proposed something still requires reproducing the turn
  locally against the deterministic fake. This is the direct cost of the
  redaction policy and is accepted.
- A trajectory records what the application observed. It is not tamper-evident:
  the JSONL sink is append-only by construction, but nothing signs or chains the
  events. A trail that must resist an attacker with write access to the sink
  needs more than this — see Phase 18.7.
- The in-memory sink is process-local and bounded, so trajectory inspection
  across replicas requires the JSONL or logging sink and a log query.
- `verify()` proves the record is well-formed. It cannot prove the record is
  complete: instrumentation that was never called leaves no event, and no
  invariant fires. Coverage of the instrumentation is a test property, not a
  runtime one.

## Related

- ADR 0008 — controlled single-agent foundation.
- ADR 0007 — evaluation, adversarial testing and LLMOps quality gates.
- OpenTelemetry GenAI semantic conventions.
