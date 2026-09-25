# ADR 0010: Agent evaluation

## Status

Accepted. Introduced by Phase 18.6 for `products/platform-engineering-assistant`.

Extends ADR 0007 (evaluation and LLMOps gates), ADR 0008 (the controlled agent)
and ADR 0009 (the audit trail). Not added to the assistant's own corpus.

## Context

Phase 17.2 built evaluation for the *answering* product: a versioned dataset, a
server-owned policy of gates, deterministic offline runs and an optional live
judged run.

An agent needs a different instrument, because the thing most worth measuring is
not the quality of an answer. It is whether the controls hold when the model
behaves badly. ADR 0008's central claim — the model proposes, the application
decides — was, before this phase, demonstrated only by unit tests written
alongside the code they test. That is the weakest form of evidence a safety
property can have.

Phase 18.5 changed what is possible: `AgentTrajectory.verify()` checks the
ordering invariants against a *recorded run*, offline and without a model. That
turns "no tool ran before policy authorised it" from an assertion about the code
into a measurement of a run — and a measurement can be a gate.

## Decision

### Each case carries a scripted proposal, and that is the whole design

Two different properties live in an agent turn, and they belong to different
parts of the system:

| | property of | measurable offline |
| --- | --- | --- |
| which tool the model chooses | the model | no |
| what the application does with any proposal | the server | **yes** |

Every safety guarantee is in the second row. So each case carries a
`scripted_decision`: the proposal the model is *taken* to have made. In fake
mode it is injected directly, which lets the suite ask the question that matters
most and answer it deterministically on every commit:

> If the model were fully compromised and proposed **this**, what would the
> application do?

Everything downstream of the proposal — policy, registry, approval, executor,
grounding, trajectory — is the shipped code, identical in both modes. The only
thing swapped is the one thing that cannot be trusted anyway.

In live mode the scripted decision is ignored, a real model proposes, and the
model-side metrics become meaningful for the first time.

### The fake/live split is sharper than in the generation suite

| gated in both modes | gated live only |
| --- | --- |
| unauthorised executions (count, bound 0) | task success |
| approval compliance | tool selection accuracy |
| policy compliance | unnecessary tool call rate |
| trajectory correctness | latency p95 |
| argument correctness | total tokens p95 |
| iteration violations (count, bound 0) | |
| tool failure recovery | |
| citation containment | |
| grounding | |
| prohibited content | |

The model gates are live-only for the reason ADR 0007 already gives about
disposition accuracy: the deterministic provider answers from the first chunk
and cannot decide a disposition. Two cases therefore score false in *every*
offline run, and that is correct rather than a defect — the report says so
explicitly, and the gate does not apply.

### Two metrics are counts, not rates

`unauthorised_execution_count` and `max_iteration_violation_count`. A rate
invites a conversation about an acceptable percentage, and there is no
acceptable percentage of a state-changing tool running without approval.

`unauthorised_execution_count` is computed from the **recorded trajectory**, by
the same rule `verify()` applies, rather than from the response. A response can
only report what the application believes it did.

### An absent trajectory scores zero

Not "not applicable". A run that recorded nothing has not demonstrated correct
ordering; it has demonstrated nothing. The same principle governs every
denominator: an empty population yields `None`, which the gate evaluator turns
into INCOMPLETE, never a pass.

### Adversarial coverage is asserted on load

Eight techniques, each pinned to at least one case by tag: prompt injection,
tool-instruction injection, risk-level tampering, approval bypass,
exfiltration, conflicting evidence, scope substitution and argument tampering.
Losing the last case for a technique is a *load failure*, not a quietly smaller
suite. Reliability adds three: timeout, malformed result, permanent failure.

None of the adversarial cases asserts that the model resisted the manipulation.
Each asserts that the deterministic controls held even though it did not.

### Misbehaviour is injected at the tool boundary

The reliability cases wrap the real tool rather than mocking the executor, so
the real timeout, the real retry classification and the real failure typing all
still run. Mocking the executor would test the mock.

The injected hostile instructions live in the runner, not in the corpus. The
corpus is the approved documentation set; planting an attack in it to test the
agent would leave the attack in the product's own evidence base.

## What the suite found

**The executor never checked what a tool returned.** The malformed-result case
was written expecting a controlled failure and instead found that an object of
the wrong type would have been passed downstream, where the grounding step reads
attributes off it. Fixed in this phase: `Tool` now declares an `output_model`,
the executor validates the returned type, and a mismatch is a new non-retryable
failure category (`MALFORMED_OUTPUT`) — non-retryable because a tool that
answers with the wrong shape will answer with the wrong shape again.

This is the suite doing its job on its first run, and it is the strongest
argument for having built it.

## LangSmith and Foundry, for evaluation specifically

ADR 0009 assessed both for *observability*. For *evaluation* the answer is
narrower and the same conclusion holds for now.

- **Deterministic offline CI is mandatory and non-negotiable.** It is free, it
  needs no credential, it runs on every pushed branch, and every gate that
  matters passes or fails in it. Nothing below may become a precondition for it.
- **LangSmith** adds dataset management and trajectory diffing across many runs.
  Both are genuinely useful once there are many runs to compare. Neither is
  usable yet: this product exports metadata only (ADR 0009), and a trajectory
  diff over enum values is a diff this repository can do with `git`. The API key
  and egress objections are unchanged.
- **Azure AI Foundry evaluation** offers hosted evaluators and a results store
  inside the Azure boundary. Its agent evaluators are built around message
  transcripts — the inputs and outputs this product does not collect — and its
  results store is new infrastructure, which this phase forbids. The natural
  future use is the *live* run: a live judged agent evaluation writing to a
  Foundry project the platform already has, with offline CI unchanged.

Neither is adopted. The seam that would make adoption cheap already exists.

## Consequences

### Positive

- The claim at the centre of ADR 0008 is now measured, on every commit, against
  hostile proposals, with a gate whose only acceptable value is zero.
- The suite found a real defect on its first run.
- Reuses the Phase 17.2 machinery unchanged: `ExecutionMode`, `Threshold`,
  `evaluate_gates`, `overall_status`, `GateStatus` and the provenance helpers
  are shared, so there is one definition of what a gate is and one of what
  INCOMPLETE means.
- Adds no runtime dependency. The product's runtime set is still five packages.

### Negative / accepted

- Twenty-two cases is a control suite, not a statistical sample. No rate here
  has a meaningful confidence interval.
- The adversarial cases enumerate the techniques this phase anticipated. An
  attack shaped differently is not covered by construction, and a clean run is
  evidence about these eight techniques only.
- Fake mode proves the application, never the model. Every model-side number in
  an offline report is a property of the fixture and is labelled as such.
- The scripted proposals are written by the same people who wrote the policy
  layer, so they test the failures that were imagined. Real traffic will propose
  things nobody scripted.
- Thresholds are initial engineering gates, not calibrated standards.

## Phase 18.7 hardening scope

Derived from what these two phases could not close.

1. **Tamper-evident audit trail.** The JSONL sink is append-only by
   construction, but nothing signs or chains events. An attacker with write
   access to the sink can rewrite history. Hash-chain each trajectory and record
   the terminal digest.
2. **Durable approvals.** `InMemoryApprovalStore` does not survive a restart,
   while Phase 18.4's workflows do. A workflow can therefore outlive the
   approval it is waiting on. The stores must have the same durability.
3. **Authentication on the approval surface.** `POST /v1/approvals/{id}/decision`
   *identifies* an approver; it does not *authenticate* one. Until it does, the
   self-approval control rests on a caller-supplied string.
4. **Trajectory retention and correlation.** In-memory inspection is
   process-local. Deciding retention, and correlating trajectories across
   replicas, is the concrete trigger for the Application Insights decision that
   ADR 0009 deferred.
5. **Live agent evaluation, and recalibration.** Every model-side gate is
   currently unexercised. A first paced live run establishes the baseline, and
   the thresholds must then be recalibrated against it and against human labels.
6. **Injection resistance as a model property.** The suite proves the controls
   hold when the model is compromised. It does not measure how often the model
   *is* compromised. That needs live adversarial runs and a judge.
7. **Rate and spend limiting.** The loop is bounded per turn. Nothing bounds
   turns per caller, so cost is bounded per request and unbounded per day.
8. **`GEN-I02` in the answering path.** Still open, inherited, and untouched by
   the agent's scope rule, which constrains tool results only.

## Related

- ADR 0009 — agent observability and audit trail.
- ADR 0008 — controlled single-agent foundation.
- ADR 0007 — evaluation, adversarial testing and LLMOps quality gates.
