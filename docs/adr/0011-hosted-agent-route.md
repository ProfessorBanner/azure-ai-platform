# ADR 0011: Hosting the controlled agent in Microsoft Foundry

## Status

Accepted. Introduced by Phase 19.1e for `labs/agent-ecosystem/hosted-agent`.

Extends ADR 0006 (the Foundry capability lab) and ADR 0008 (the controlled
agent). Does not supersede ADR 0008: the control model is unchanged, only its
host. Not added to the assistant's own corpus.

## Context

ADR 0008 established the claim this platform's agent work rests on: **the model
proposes, the application decides.** Registry, policy, bounded execution,
approval and audit are the application's, and no model output reaches an
external effect without passing them.

Phase 19 asked whether that claim survives being hosted by someone else.
Microsoft Foundry offers two routes:

- **Prompt Agent** — Foundry holds the instructions and tool contract, calls the
  model, and composes the final answer inside its own service.
- **Hosted Agent** — Foundry runs *your* container and calls into it over the
  Responses protocol.

Phase 19.1b established the decisive difference. A Prompt Agent composes the
answer inside Foundry, where the application cannot inspect it — so Phase 18's
server-side citation rebuilding and fail-closed refusal do not survive. The
grounding guarantee is lost precisely because the last step happens somewhere
the application does not run.

## Decision

### The Hosted route, because a control must run where the model call runs

The controlled agent is packaged as a container and hosted. Foundry provides
compute, scaling, routing, identity and the session lifecycle; the container
provides the agent. Every Phase 18 control executes in the same process as the
model call, so none of them is delegated.

The price is accepted deliberately: the platform now owns an image, its base
layers and its dependency drift, and the deploy loop is slower than editing a
prompt. Prompt Agents remain the right default for orchestration one is content
to delegate. Hosted is what one reaches for when a control must not be.

### The protocol is the platform's, not ours to imitate

The first cut of 19.1e hand-built a Responses payload. That was a plausible
guess at a contract the platform owns, and it was deleted rather than maintained
alongside the real thing. `azure-ai-agentserver-responses` now serves
`/responses`, the response lifecycle, cancellation, the response store and
`/readiness`. The package contributes two pure functions — the assistant text
and the structured metadata — and a handler.

The approval routes remain mounted on that server and served from the product's
own `ApprovalService`. **Foundry hosts the process; it does not hold the
approval record and cannot decide one.** Adopting the official protocol server
did not move that boundary, and that was the condition for adopting it.

### The image is a repository export, not a package install

The product resolves `PRODUCT_ROOT` from its own `__file__` and reads its
retrieval config, prompts and corpus manifest from the repository beside it. The
corpus manifest names documents **repository-relative**, and the loader derives
its containment root from a `.git` entry and refuses anything outside it.

So the container carries `products/`, `labs/`, `docs/` and a repository-root
marker, and installs editable at `/app`. Two consequences follow, and both were
learned the expensive way:

- **The build and runtime stages must share one absolute path.** An editable
  install records an absolute path in a `.pth` file. Building at `/build` and
  relocating to `/app` leaves those paths dangling: the distribution is
  installed and the module is unimportable. Version 1 shipped exactly this and
  failed every session with `No module named hosted_agent`.
- **Every check that matters must run against the artefact.** Version 1 passed
  format, lint, strict types, tests and an in-process smoke run, because all of
  them ran in a virtualenv where the module was importable by construction.
  `scripts/container_smoke.sh` builds the image, starts it under its declared
  `CMD`, and polls `/readiness` — and is the gate before any push.

Making the product a self-contained distributable is real work, recorded as a
prerequisite for any hosting route that does not carry the repository.

### Platform-owned inputs are asserted, not guessed

`PORT` is reserved by Foundry and injected; the manifest must not declare it and
the image must not bake it. `cpu` and `memory` are a **pair** — Foundry accepts
a fixed set of tiers and rejects any other combination, so `1` with `4Gi` is
invalid even though both values appear in the table. Architecture is asserted
with `TARGETARCH` inside the build rather than pinned with a constant
`FROM --platform`, which would override the build's own `--platform` and produce
a mixed image silently. `hosted_deploy.load_manifest` enforces all three before
anything is sent.

### Runtime-identity RBAC is a post-deployment step, by necessity

Foundry mints the agent's runtime managed identity **when the version is
created**. Before that there is no principal to grant to. The inference role is
therefore granted by an idempotent script that resolves the principal from the
live version, not by Terraform, and the live principal is committed nowhere.

The trap this closes: omitting the grant does not fail the deployment. The
version goes `active`, the container starts, `/readiness` returns 200 — and
every turn then fails. Health and capability are not the same property.

## Consequences

### What this buys

Grounding and approval enforcement survive hosting, because they run in the
process that makes the model call. Proven live on version 2: grounded answers
with citations; a `state_changing` tool held at `not_executed` behind an
application-held `approval_id`; and an out-of-scope refusal.

### What it costs

An image to patch, a slower deploy loop, and a failure surface that spans three
layers. Known failures return a **200** with a named `failure_category`;
unexpected defects return a **200** with `internal_error` and a correlation id,
with the traceback confined to the log. A **500** is the platform's, and can
mean the agent *succeeded* and Foundry could not store the result — the response
store is an outbound call that a firewall can block after the handler has
already returned.

### What is not solved

**The default-deny network path.** The account normally runs
`defaultAction: Deny` with an operator IP allowlist, and under that
configuration the Hosted Agent cannot reach either the model data plane or
Foundry response storage. `bypass: AzureServices` did not admit the traffic and
is deliberately not adopted: it widens exposure without solving the path.

The live proof required temporarily setting `defaultAction: Allow`, which was
restored to `Deny` immediately afterwards. **This ADR does not claim the account
works under default-deny.**

Production-grade Hosted Agent networking needs **VNet injection configured when
the Foundry account is created**; Microsoft does not support adding it later.
Retrofitting is therefore impossible on `aif-example-sandbox` and requires a
new account and capability root. Recorded as a future infrastructure
prerequisite, not as work completed here.

## Alternatives considered

**Prompt Agent.** Rejected for this agent: the final composition happens inside
Foundry, so citation rebuilding and fail-closed refusal cannot run. Still the
right default where those controls are not required.

**Code packaging (`CodeConfiguration` + `remote_build`).** Rejected: it resolves
dependencies at deploy time, which is the drift the product's `uv sync --frozen`
discipline exists to prevent.

**A global `PYTHONPATH` to fix the import.** Rejected: it papers over a
relocation bug and would have left the corpus failure undiscovered behind it.
The packaging contract was fixed instead.
