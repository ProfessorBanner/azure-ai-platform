# Foundry agent lab (Phase 19)

A bounded comparison lab: the same question, the same corpus, the same retrieval
controls, orchestrated by **Microsoft Foundry** instead of by the Phase 18
application.

It exists to answer one question — *what actually moves when orchestration
becomes managed, and what must not* — and it is deliberately not a product. It
has no environment, no promotion path and no pipeline. It does not replace
`products/platform-engineering-assistant` and nothing in that product imports
it.

## Phase 19.1b — one read-only tool

```
1. Foundry Prompt Agent selects the tool          MANAGED
2. this lab validates the arguments               APPLICATION-OWNED
3. this lab executes the trusted read-only search APPLICATION-OWNED
4. this lab returns function_call_output          APPLICATION-OWNED
5. Foundry composes the final response            MANAGED
```

One tool, `search_platform_docs(query: str)`, read-only. No state-changing tool
is registered — and cannot be: `LabToolRegistry` refuses one at construction,
because the approval machinery that would make it safe lives in the product and
has not been ported here.

## Responsibility split, as built

| Concern | Owner | Note |
|---|---|---|
| Model invocation | **Foundry** | Deployment named in the agent version |
| Agent definition | **Foundry** | `PromptAgentDefinition` |
| Agent versioning | **Foundry** | `agents.create_version` — a new version per tool contract |
| Decision loop | **Shared** | Foundry proposes; the lab decides whether to act and when to stop |
| Tool schema | **Application** | Hand-built, strict, `additionalProperties: false` |
| Tool dispatch | **Application** | Nothing executes without passing validation |
| Argument validation | **Application** | Typed rejections; bounds in code, not in the schema |
| Risk classification | **Application** | Registry-owned; never sent to Foundry, never read back |
| Conversation / session | **Foundry** | Conversation id returned and recorded |
| Turn record | **Application** | Conversation id, response ids, tool, validated arguments |
| Corpus + retrieval | **Application** | Reused unchanged from the product |
| Call bounding | **Application** | `MAX_TOOL_CALLS`, clamped, `for` not `while` |
| Grounding enforcement | **NOT PROVIDED** | See below — the headline finding |
| Approval / HITL | **NOT PROVIDED** | Out of scope for 19.1b |
| Policy layer | **NOT PROVIDED** | Validation only; no allow-list ruling beyond registration |

## The headline finding

**Grounding enforcement does not survive the move.** Phase 18 rebuilds citations
server-side from trusted chunk metadata and refuses fail-closed when an answer
is not supported by retrieved evidence. In this arrangement the model composes
the final answer *inside Foundry*, from evidence the application handed over as
text. The application never sees the draft, so it cannot check it.

What the lab still controls: which evidence exists, whether a tool runs at all,
and what the arguments were. What it has given up: any guarantee that the answer
rests on that evidence. That is the real cost of managed composition, and it is
the thing to weigh — not the SDK ergonomics.

Two smaller consequences, both recorded rather than worked around:

* **Redaction is inverted.** Phase 18 records an argument *fingerprint* and never
  the arguments. This lab records the validated arguments, because a comparison
  whose own record is redacted cannot show which side lost information. That is
  lab-only: a query is the user's question restated, so this record must not be
  shipped to a durable sink without revisiting the decision.
* **Reuse is by source, not by dependency.** `azure-ai-projects==2.5.0` requires
  `openai>=3`; the product pins `openai>=1.99,<3`. The intersection is empty, so
  the lab cannot depend on the product package. It puts the product's `src` on
  the import path instead — safe only because the reused subgraph imports
  nothing outside `pydantic`, which `tests/test_product_reuse.py` asserts. The
  production fix, if this ever graduates, is to extract corpus + retrieval into
  a package both sides depend on.

## Running it

Offline — no Azure, no credential, deterministic. This is what CI can run:

```bash
uv run --directory labs/agent-ecosystem/foundry-agent \
  python -m foundry_agent_lab.smoke --offline \
  --question "How is Terraform state separated between environments?"
```

Live — calls a metered deployment, so it is manual and never automatic:

```bash
export FOUNDRY_PROJECT_ENDPOINT="https://aif-example-sandbox.services.ai.azure.com/api/projects/proj-capability-lab"
export FOUNDRY_AGENT_NAME="phase19-docs-agent"       # optional
export FOUNDRY_MODEL_DEPLOYMENT="gpt-4-1-mini"        # optional

az login   # Entra ID; the account has local auth disabled

uv run --directory labs/agent-ecosystem/foundry-agent \
  python -m foundry_agent_lab.smoke \
  --question "How is Terraform state separated between environments?"
```

The live path requires the caller's egress address to be in the Foundry
account's IP allow-list — the account is default-deny.

Tests:

```bash
uv run --directory labs/agent-ecosystem/foundry-agent pytest -q
uv run --directory labs/agent-ecosystem/foundry-agent mypy src tests
```

## Phase 19.1c — approval-gated state change

`propose_change_request` is exposed to Foundry as a function tool, and every
proposal is routed through the **Phase 18** registry, policy layer, approval
service and executor. The lab supplies the adapter and the sequencing; it makes
no ruling of its own.

```
Foundry proposes  ->  AgentDecision  ->  product authorise()  ->  verdict
                                                                   |
                          ALLOW (read-only) -> product executor -> evidence
                          REQUIRE_APPROVAL  -> approval recorded, NOTHING runs
                          DENY              -> refused
```

### Foundry HITL vs application-owned business authority

Foundry has its own approval-shaped features. They are not what this lab uses,
and the distinction is the point of 19.1c.

| | Foundry-managed HITL | This lab / Phase 18 |
|---|---|---|
| What it gates | a step in a managed flow | a specific ACTION with specific arguments |
| Who decides risk | the flow author, at design time | the server registry, at runtime, per tool |
| What approval binds | the run | tool name + SHA-256 of the validated arguments |
| Expiry | flow-dependent | evaluated at execution time, not by a sweeper |
| Who may approve | whoever holds the surface | not a machine principal, not the requester |
| Replay safety | run-scoped | recorded call id + idempotency claim |
| Where the record lives | the platform | the application, joinable to its own audit |

The reason to keep it application-owned is not distrust of Foundry. It is that
**approval is a business invariant, not a workflow step**. "This exact change,
approved by this identified human, still valid at the moment it runs" is a
statement about the organisation, and it has to survive the orchestration being
swapped. An approval that lives inside the runtime is only as portable as the
runtime.

What Foundry genuinely owns here: proposing the action, and telling the user it
was recorded rather than performed. It is told `APPROVAL_REQUIRED` as a function
output so it can say so honestly.

### What 19.1c proves offline

Deterministic tests cover approval bypass, arguments changed after approval,
denial, expiry, duplicate execution, self-approval, machine-principal approval,
unknown tools, and a compromised model attempting risk manipulation. That last
one is worth naming: a function call has no field for risk, so the only way to
assert one is to smuggle it in as an argument — and the tool's closed input
model rejects it before policy is consulted.

### Live commands

```bash
# offline, deterministic
uv run --directory labs/agent-ecosystem/foundry-agent \
  python -m foundry_agent_lab.govern_cli --offline \
  --approve-as manual-local-human \
  --question "Please raise a change request to increase sandbox Foundry capacity to 30."

# live (metered; requires az login and an allow-listed egress address)
export FOUNDRY_PROJECT_ENDPOINT="https://aif-example-sandbox.services.ai.azure.com/api/projects/proj-capability-lab"
uv run --directory labs/agent-ecosystem/foundry-agent \
  python -m foundry_agent_lab.govern_cli \
  --approve-as manual-local-human \
  --question "I want to formally propose a platform change for review - raise sandbox Foundry capacity to 30."
```

The four stages print in order: provisioning, the proposal and its verdict, the
recorded approval, the human decision, and the resume. Omit `--approve-as` to
stop after the approval is recorded and prove nothing executed.

Both stages share one process because the approval and workflow stores are
in-memory — the same limitation Phase 18 carries and records as open. Splitting
them across shell invocations would lose the state, and faking durability with a
temp file would hide the gap rather than close it.

### An honest note on the resume outcome

`propose_change_request.run` raises by design — the Phase 18 tripwire that fires
if anything ever routes past the policy layer. The approved resume path legitimately
reaches it, so the execution FAILS. This lab records that honestly: the workflow
moves to `failed`, the call is not recorded as performed, and the idempotency
claim is retained so whether it may repeat stays a human decision. Phase 18's own
resume records the same situation as `answered`/`completed`, which is the open
defect noted at the end of Phase 18.

## Phase 19.1d — lifecycle, correlation and evaluation

### Correlation

One record per turn joins the managed and application halves, which are
otherwise only joinable by timestamp — and a timestamp is not a join.

| Recorded | Owner |
|---|---|
| `agent_name`, `agent_version` | Foundry |
| `conversation_id`, `response_ids` | Foundry |
| tool name, outcome, risk, policy decision, denial reason | Application |
| `approval_id`, `argument_fingerprint` | Application |
| `workflow_ids`, `outcome` | Application |

**Not recorded:** chain-of-thought (never collected, so it cannot leak),
credentials, endpoints, tokens, instruction text, or the user's question — which
is carried as a length. `validated_arguments` IS recorded, deliberately and
lab-only, because a comparison whose own record is redacted cannot show which
side lost information; that must be revisited before any durable sink.

`correlation.verify()` is the trajectory-validity check: it is deterministic,
offline, and used as an evaluation gate rather than only as an assertion.

### Foundry tracing vs the Phase 18 safe trajectory

| | Foundry tracing | Phase 18 trajectory |
|---|---|---|
| Shape | OTel spans over the managed run | ordered typed events per turn |
| Content | prompts, tool arguments, model output by default | identifiers, enums, counts, hashes only |
| Redaction | opt-out, and the default is verbose | structural — the fields do not exist |
| Where it lands | Azure Monitor / App Insights | whatever sink the application configures |
| What it can prove | that a step happened, and how long it took | that no tool ran before policy authorised it |
| Verifiable offline | no | yes — `verify()` needs no clock, I/O or model |

They answer different questions and are complementary rather than competing.
Foundry tracing is the better view of *the managed runtime's own behaviour* —
latency, retries, model calls — inside the Azure boundary and with managed
identity. It cannot replace the safe trajectory, because its default posture is
to capture exactly the content this platform has decided not to collect, and
because an ordering invariant checked by a person reading a waterfall is not a
gate.

This lab adds no observability infrastructure. Adopting Foundry tracing needs an
Application Insights resource, which Phase 18 deferred and Phase 19 has not
revisited.

### Agent provisioning, immutable versions and runtime routing

Version creation is **not** part of serving a turn. `provisioning.py` is the only
code that writes to the agent definition, and it is not reachable from the
serving path. Three reasons:

* a request that should only consume the platform should not be able to change
  it;
* one version per turn makes "which definition produced this answer"
  unanswerable — the versions become noise;
* serving availability should not depend on the definition endpoint.

`agents.create_version` produces a new immutable version rather than editing one
— the managed equivalent of the prompt version and content hash the Phase 18
product ships with every response. The version is passed into the loop and
recorded on every turn, so an answer is always attributable to the exact
instructions and tool contract that produced it. Runtime routing consumes an
existing version and never writes.

### Evaluation

`evaluation/agent_lab_v1.json` — five versioned cases: direct answer, correct
read-only tool use, unnecessary tool avoidance, approval-required action, and an
adversarial case combining injection, approval bypass and risk manipulation.
Gates live in `evaluation/gates_v1.json` and are loaded with the **Phase 18**
policy loader, so both stacks agree on what a threshold is and what INCOMPLETE
means.

```bash
uv run --directory labs/agent-ecosystem/foundry-agent \
  python -m foundry_agent_lab.evaluation.cli --mode fake
```

Exit codes match the Phase 18 suites: `0` pass, `1` gate failed, `2` incomplete,
`3` misconfiguration. Incomplete fails too — "we did not measure this" must not
be indistinguishable from success.

Control gates (`policy_compliance`, `approval_compliance`,
`unauthorised_execution_count`, `trajectory_validity`, `task_success`) apply in
**both** modes, because with a scripted proposal the outcome is fully determined
by the controls. Model gates (`tool_selection_accuracy`,
`unnecessary_tool_call_rate`) are **live-only** and are listed in every offline
report as explicitly not assessed.

## Status

`foundry.py` — the real adapter — was written against the **installed** SDK
(`azure-ai-projects==2.5.0`), introspected rather than taken from documentation:
`client.agents` is `AgentsOperations`, `PromptAgentDefinition` carries
`model`/`instructions`/`tools`, `FunctionTool` accepts the parameter schema
directly, and `get_openai_client(agent_name=...)` returns an `openai.OpenAI`
bound to the agent's endpoint — so the turn protocol is the Responses API.

**It has not been run against Azure.** Phase 19.1b was implemented under an
instruction not to call Azure, so the round trip is unproven and the adapter
should be treated as unverified until the live command above is run.
