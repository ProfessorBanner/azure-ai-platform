# ADR 0008: Controlled single-agent foundation

## Status

Accepted. Introduced by Phase 18.1 for `products/platform-engineering-assistant`.

Not added to the assistant's own corpus. Corpus membership is out of scope for
this phase, and a document about how the agent is built is not documentation the
agent needs in order to answer questions about the platform.

## Context

Phase 17 built a grounded question-answering product: an approved corpus, BM25
retrieval, versioned prompts, fail-closed grounding enforced outside the model,
server-built citations, and an evaluation suite with deterministic gates.

Phase 18 asks a different question: what happens when the model can *act*?

Everything that made Phase 17 safe rests on one property — the model's only
influence over the response is what to say and which supplied chunks support it,
and both are checked afterwards. An action has no equivalent "afterwards". A
tool that has run has run.

## Decision

### Separation of powers, and where each power lives

```
LLM              proposes          untrusted, structured, no authority
Policy layer     authorises        deterministic, pure, registry-driven
Tool registry    owns risk         server-side, immutable at runtime
Tool             executes          typed input, typed output, explicit scope
Application      controls the loop bounded, explicit, records the outcome
```

The model proposes. The application decides. Every type in `agent/domain.py` is
shaped by that split: `AgentDecision` is a proposal and carries no authority,
while `PolicyVerdict` and `AgentResponse` are server-produced and are the only
things a caller may act on.

### Risk is a property of the tool, never of the proposal

`ToolRiskLevel` is read from the tool registry and from nowhere else. The
registry is built in server-side code at startup and is immutable afterwards.

A model that announces the state-changing tool is "read only" changes nothing.
The claim is recorded in telemetry — so that a systematic attempt to
misclassify tools is *observable* rather than invisible — and the policy layer
consults the registry. This is the single most important invariant in the agent
layer. It is tested behaviourally, and enforced against the parsed AST of
`policy.py` so that a future edit reintroducing the field fails the suite.

The tool catalogue shown to the model deliberately omits risk levels. Risk is
not the model's business, and showing it would invite an argument the policy
layer does not participate in.

### Policy is deterministic, and never asks another model

`authorise` is a total function from (proposal, registry) to a verdict, with no
I/O, no clock and no randomness.

Asking an LLM whether a tool call is safe would produce a guess dressed as a
ruling, from the same class of system whose proposal is being checked, and
susceptible to the same prompt injection that may have produced the proposal.
An attacker who can influence the proposal can influence the reviewer. A lookup
in a version-controlled table cannot be talked round.

Rules, in order — registration is checked before arguments, because an unknown
tool has no input model to validate against and attempting one would be the
first step towards inventing it:

    refuse / no_tool          -> ALLOW
    tool name missing         -> DENY
    tool not registered       -> DENY
    tool not allow-listed     -> DENY
    arguments do not validate -> DENY
    READ_ONLY                 -> ALLOW
    STATE_CHANGING            -> REQUIRE_APPROVAL

Every unrecognised condition denies. Adding a third risk level fails closed
rather than falling through.

### Registration and permission are separate

A tool can be registered and not allowed. Collapsing the two would mean
disabling a tool required deleting its definition — and a deleted definition
takes its risk classification with it.

### Scope travels with the fact

Where a tool returns environment- or scope-specific information, the scope is a
**field** (`EvidenceScope`: environment, component, authority, source documents),
not something a reader is expected to infer from prose.

A fact detached from its qualifier reads as universally true. "Capacity is 10"
is a different statement from "sandbox capacity is 10", and when the qualifier
lives in a different chunk, a model asked about production will answer with the
sandbox number and cite a real source while doing it. Phase 17's `GEN-I02`
finding demonstrated exactly that failure in the answering path.

So `lookup_platform_component` **refuses rather than substitutes**: asked for an
environment it has no evidence for, it returns `found=False`,
`unsupported_environment=True`, and the list of environments it does have. It
never falls back to another environment's evidence.

This is a general agent-design rule, not a fix for one evaluation case. There is
no case-specific logic anywhere in the agent layer. `_environment_of` returns a
scope only when a chunk names exactly one environment — a chunk comparing all
four is not evidence about any one of them — and matches on word boundaries so
that "dev" never fires on "developer".

### Approval is an outcome, not an error

`approval_required` is a first-class `AgentOutcomeKind` returned with HTTP 200.
Modelling it as a 4xx would teach callers to retry the one thing that must not
be retried without a human.

`propose_change_request` performs no mutation of any kind. In Phase 18.1 its
`run` is never reached, because policy stops it first; it nonetheless raises
rather than returning quietly, so a refactor that ever routed past the policy
layer fails loudly. A test replaces it with a tripwire and asserts it is never
called.

The approval summary a human reads is assembled by the **server** from the
validated typed input — never from prose the model wrote.

### Grounding is unchanged

Answers are produced by the existing pipeline: existing prompt, existing
`build_context`, existing `enforce_grounding`, citations rebuilt server-side
from trusted chunk metadata. A tool contributes **which chunks are considered**
— never what may be said about them, and never a citation. The fail-closed
grounding tests are re-run against the tool-grounded path to prove it.

Tool results are structured evidence that the application labels and bounds. A
tool result is never spliced into a prompt as free-form text.

### The loop is bounded by construction

`max_tool_iterations = 2`: consult a tool, then answer from what it returned.
The ceiling is enforced in the service and clamped against a module constant, so
a caller-supplied 99 is silently reduced rather than honoured. An unbounded
agent loop against a metered model is a cost incident waiting for one bad
decision to trigger it.

## Why no framework, and what would change that

### LangChain — unnecessary now

Its value is adapters, and this product has one provider, one retriever and
three tools, all of which it already owns. What it would cost is the thing that
matters: the control flow *is* the security property here. A reader must be able
to confirm, by reading one file, that no path reaches `tool.run` without passing
`authorise`. A framework hides exactly that step behind its own dispatch.

Revisit when the number of providers or tool integrations makes hand-written
adapters the larger cost.

### LangGraph — assessed in Phase 18.4 and REJECTED for now

Phase 18.4 built exactly the thing LangGraph is for — durable, resumable
workflow state with an approval wait — and assessed the library against real
dependency metadata rather than reputation. It does not earn its place yet:

  * `langgraph` requires `langchain-core`, which requires **`langsmith`** as a
    hard, non-optional dependency. Adopting LangGraph therefore installs
    LangSmith, which this ADR defers below and which is a third-party
    data-egress decision, not a library choice.
  * The transitive addition is thirteen or more packages into a product whose
    entire runtime dependency set is five.
  * Its durable checkpointers are SQLite or Postgres. Postgres would be new
    Azure infrastructure, which Phase 18 forbids; SQLite is a file, which is
    what `JsonFileWorkflowStore` already is.
  * The graph is six live states, one wait state, and a ceiling of two tool
    iterations. `ALLOWED_TRANSITIONS` is a table that fits on a screen.

What LangGraph would genuinely add — and what would justify revisiting it — is
concurrent fan-out, streaming intermediate state to a UI, and time-travel
debugging across long trajectories. None of those exist yet.

### LangSmith — deferred until agent trajectory evaluation

Phase 17.2 built report-based evaluation with versioned gates and structural
redaction. LangSmith addresses trajectory observability across multi-step runs,
which a two-iteration loop does not yet have. It would also send prompts and
tool arguments to a third party, which needs a deliberate data-egress decision
rather than a dependency.

### Multi-agent — deferred

There is one job here: answer a documentation question, possibly after
consulting one tool. Multiple agents multiply the surface on which an
unauthorised action could be proposed and add inter-agent messages as a new
injection channel, in exchange for capability this phase does not need.

### Real state-changing integrations — deferred

Approval persistence, Azure DevOps writes, retries, idempotency and compensation
all belong to later Phase 18 stages. A tool that genuinely mutates something
must not be added until approval can be recorded and audited, which requires the
durable state noted above.

## Consequences

### Positive

- No tool executes without a deterministic, unit-testable authorisation.
- A state-changing action cannot be triggered by model output alone, whatever
  the model asserts about it.
- Scope is structural, so a qualifier cannot be lost in transit between a tool
  and an answer.
- The whole agent path is provable offline: no Azure, no credential, no network.
- Phase 18.1 adds an architecture and no new runtime dependency.

### Negative / accepted

- Two model calls per tool-using turn, so a tool-using answer costs roughly
  twice a direct one.
- `_environment_of` is lexical. It relies on the platform's closed
  four-environment vocabulary and will not detect a scope expressed in words it
  does not know. It is a control on the tool boundary, not a general
  scope-inference engine.
- Approval terminates the turn with no way to resume, which is a deliberate
  limitation and the main driver for Phase 18.2.
- The agent inherits every Phase 17 limitation, including the open `GEN-I02`
  scope-leap finding in the *answering* path. The scope rule here constrains
  tool results; it does not constrain what the model may say about corpus chunks
  retrieved directly.

## Related

- ADR 0007 — evaluation, adversarial testing and LLMOps quality gates.
- `products/platform-engineering-assistant/README.md`.
