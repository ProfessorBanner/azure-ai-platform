# ADR 0006: Microsoft Foundry capability lab

## Status

Accepted

## Context

Phases 7–15 built a four-environment Azure/Databricks platform: reusable
modules, one Terraform state per environment, VNet-injected workspaces, Unity
Catalog, and a governed `dev -> stg -> prod` promotion chain with a mandatory
PROD approval.

Phase 16 explores a different class of capability: a managed generative-AI
service. Phase 17 will build `products/enterprise-llm-demo` as a
provider-neutral FastAPI/RAG application, and Phase 18 will extend it into a
controlled agentic system. Phase 16 exists to answer the infrastructure,
identity, networking, telemetry and cost questions **before** an application
depends on the answers, and to publish a stable service contract Phase 17 can
consume.

Discovery (Phase 16.0) established the relevant facts: the `azurerm` provider
pinned at 4.81.0 exposes `azurerm_cognitive_account`,
`azurerm_cognitive_account_project` and `azurerm_cognitive_deployment`;
`Microsoft.CognitiveServices` is registered; UK South offers the `S0` SKU for
kind `AIServices` with all model quota unconsumed; and the sandbox resource
group already provides a Log Analytics workspace with a daily ingestion cap.

## Decision

### Use the modern Foundry resource model, not the legacy AI Hub

A Foundry resource is an `azurerm_cognitive_account` with `kind = "AIServices"`
and `project_management_enabled = true`, carrying
`azurerm_cognitive_account_project` children and
`azurerm_cognitive_deployment` model deployments.

The legacy alternative, `azurerm_ai_foundry` (the classic AI Hub), is an Azure
Machine Learning workspace: its schema **requires** `key_vault_id` and
`storage_account_id` and it emits a `workspace_id` and `discovery_url`. Adopting
it would drag a Key Vault, a storage account and a container registry into scope,
cost materially more, and produce none of the project-endpoint contract Phase 17
needs. No repository evidence supports a compatibility exception. It is rejected.

### Foundry is isolated from the four platform environment roots

Foundry lives in its own **capability root**,
`infrastructure/capabilities/ai-foundry/sandbox`, with its own state key
`capabilities/ai-foundry/sandbox.tfstate` — deliberately not under
`infrastructure/environments/`.

Three reasons:

1. **Blast radius.** The four environment roots manage durable platform
   substrate. A metered inference deployment is an experiment with a live meter.
   Separate state means the lab can be planned, applied and destroyed without a
   plan ever touching `platform/{sandbox,dev,stg,prod}.tfstate`.
2. **Semantics.** The environment roots share a stable, deliberately uniform
   output contract instantiated four times. Foundry exists in exactly one place
   and is not a tier of anything; forcing it into that contract would either
   pollute all four roots or fork the one root that differs.
3. **Lifecycle.** Teardown must be trivial and safe. An independent state makes
   `terraform destroy` on this root a bounded operation.

The capability still composes with the platform rather than duplicating it: the
sandbox resource group and Log Analytics workspace are read through **data
sources by name**, not `terraform_remote_state`. That keeps the coupling
one-directional and read-only, and leaves the capability independently
destroyable.

### Phase 16 starts in sandbox

Per ADR 0005, sandbox is a separate environment *class* for human
experimentation with disposable data, weaker durability and no production
credentials — not a promotion stage. An exploration whose purpose is to discover
the correct model, capacity, network posture and RBAC belongs there by
definition. Nothing about Phase 16 is a governed workload, so nothing about it
should enter the `dev -> stg -> prod` chain. A DEV/STG/PROD Foundry rollout is an
explicit non-goal.

### Target the OpenAI-compatible Responses API v1

The published contract is `openai-responses-v1`: an OpenAI-compatible base URL
ending in `/openai/v1/`, with the Entra scope `https://ai.azure.com/.default`.

Responses is the current, forward-looking API surface. It carries the primitives
Phase 18's agentic work will need — server-held conversation state, structured
output and tool calling — as first-class concepts, where Chat Completions treats
them as bolt-ons. Designing Phase 17's provider adapter against Chat Completions
would mean rewriting it at Phase 18. The OpenAI-compatible `/openai/v1/` base
additionally means Phase 17's adapter is a thin configuration layer over a
standard client, which is what keeps the provider genuinely swappable.

### Regional Standard, gpt-4.1-mini, capacity 1

The deployment is `gpt-4.1-mini` version `2025-04-14`, SKU `Standard`, capacity
1, `version_upgrade_option = "NoAutoUpgrade"`.

- **Regional `Standard` over `GlobalStandard`**: `Standard` keeps inference
  inside UK South, matching the platform's default-region rule and keeping the
  data-residency story simple. Discovery confirmed `gpt-4.1-mini` is the only
  small, current chat model in UK South that offers a regional `Standard` SKU at
  all — `gpt-4o-mini`, `gpt-4.1-nano` and the `gpt-5` family are GlobalStandard
  only there. `GlobalStandard` remains available later if throughput, cost or
  model choice justifies giving up residency; that would be a deliberate,
  documented change.
- **Capacity 1** (1,000 tokens/minute) is the smallest useful guardrail. Quota is
  entirely unconsumed (0/200 TPM for this model and SKU), so capacity can be
  raised without a quota request.
- **`NoAutoUpgrade`** pins the served version. Evaluation results and captured
  token/latency figures are meaningless if the model changes underneath them.

### Public endpoint with default-deny IP ACL precedes private connectivity

The account is created with `public_network_access_enabled = true` and
`network_acls.default_action = "Deny"`, reachable only from explicitly supplied
IPv4 CIDRs. `local_auth_enabled = false` disables API keys entirely, so Entra ID
is the only accepted credential and no secret exists anywhere in the design.

Private endpoints are deferred, not dismissed. Discovery established that the
sandbox VNet has a free private-endpoint subnet but **none** of the three
`privatelink` DNS zones Foundry requires, and the exact zone and `group_id` set
for a project-enabled AIServices account could not be confirmed from a
read-only query because no such account exists yet to inspect. Committing a
private-link design on an unverified assumption would repeat a mistake the
platform has already avoided elsewhere: `modules/storage_private_access` exists
precisely because that path was proven at runtime first and codified second.

There is also a hard constraint. Foundry has no VPN or Bastion path into
`vnet-aiplatform-sandbox`, so a private-endpoint-only account would be
unreachable from a workstation without new gateway infrastructure and new cost —
scope Phase 16 explicitly excludes. Meanwhile the default-deny public endpoint
serves the two consumers that matter: a workstation via its egress address, and
VNet-injected Databricks compute via its single stable NAT egress address.

The accepted consequence is that **Microsoft-hosted Azure DevOps agents cannot
reach the data plane** — their egress ranges are large and rotating, and
allow-listing them would defeat the control. No data-plane smoke test is
therefore added to hosted CI, and no self-hosted agent is introduced. Control-
plane Terraform is unaffected, because it talks to Azure Resource Manager.

### RBAC is documented and applied manually, for now

No `azurerm_role_assignment` is declared in this capability.

Every deployment identity (`id-azdo-aiplatform-tf-apply-*`) holds **Contributor**,
which excludes `Microsoft.Authorization/roleAssignments/write`. Terraform-managed
role assignments would fail with `AuthorizationFailed`. The alternative —
granting a CI identity **Role Based Access Control Administrator** — increases
the privilege of an automated identity, which is the opposite of what this phase
is trying to establish, and is not a change to make casually inside an
exploratory phase.

So Phase 16 documents the required grants and applies them by hand under the
existing human-approval rule for `az role assignment create`:

| Principal | Role | Role definition ID | Scope |
|---|---|---|---|
| Human operator and runtime caller | `Foundry User` | `53ca6127-db72-4b80-b1b0-d745d6d5456d` | Foundry **account** resource |

**Corrected in Phase 16.2.** This ADR originally named `Cognitive Services User`,
carried over from classic Azure-OpenAI-only accounts. That is the wrong role for
this resource: what Phase 16.1 deployed is a Microsoft Foundry account with
projects, and `Foundry User` is the role that matches it. Neither
`Cognitive Services User` nor `Cognitive Services OpenAI User` should be assigned
here.

The distinction that matters is control plane versus data plane. Azure separates
`actions` (manage the resource) from `dataActions` (call the resource). Owner and
Contributor grant `actions: ["*"]` and declare **no `dataActions`**, so an
inherited Owner or Contributor assignment lets a principal create, reconfigure
and delete this Foundry account while still being refused on every inference
call. `Foundry User` carries `dataActions: ["Microsoft.CognitiveServices/*"]`,
which is what actually authorises the call. Inheriting Owner is therefore not a
substitute and will not clear an authorization failure.

Scope the assignment to the Foundry account resource, not the subscription or
resource group. Runtime principals must never receive `Azure AI Administrator`
or `Cognitive Services Contributor`.

This is explicitly an interim position. Whether RBAC becomes Terraform-managed —
and at what cost in CI privilege — is a Phase 17 decision.

### Applying the capability requires an explicit human action

The capability has its own manual-only CD pipeline
(`azure-pipelines/terraform-cd-foundry-sandbox.yml`) with `trigger: none` and
`pr: none`. The platform CD pipeline applies sandbox automatically on merge to
`main`; a metered inference deployment must not inherit that behaviour. The
pipeline reuses the shared `templates/terraform-cd-stages.yml` unchanged, so it
keeps every existing control — read-only plan, destructive-change guard,
exact-binary-plan promotion, Environment approval, apply of the reviewed plan
verbatim, post-apply drift check — and differs only in trigger policy.

## Consequences

### What Phase 17 consumes

A five-value contract, published as Terraform outputs and intended to reach the
application as flat configuration:

| Output | Meaning |
|---|---|
| `llm_endpoint` | OpenAI-compatible v1 base URL |
| `llm_project_endpoint` | Foundry project endpoint, where the project publishes one |
| `llm_deployment` | the identifier a client sends as its model |
| `llm_auth_scope` | `https://ai.azure.com/.default` |
| `llm_api_contract` | `openai-responses-v1` |

No secret appears in the contract, and none exists to appear. Phase 17 owns its
`FoundryLLMProvider` adapter; these five values are the entire coupling surface,
and no Foundry, Cognitive Services or Azure vocabulary may reach Phase 17's
domain or API layers. Richer operational outputs (resource IDs, principal IDs,
model metadata, SKU and capacity, diagnostic setting ID, network mode) exist for
platform engineering, not for the application.

### Positive

- The Foundry meter is isolated in its own state and behind a manual gate.
- No secret material exists in the design; Entra ID is the only credential.
- The deployment is pinned and minimally sized; quota is untouched.
- Phase 17 can be built against a stable, provider-neutral five-value contract.

### Negative / accepted

- Hosted CI cannot exercise the data plane; runtime proof is a local or
  Databricks-side activity.
- The IP allow-list needs updating when a workstation egress address changes.
- RBAC is manual, and therefore neither reproducible nor drift-checked, until a
  later phase revisits it.
- `azurerm_cognitive_account_project.endpoints` is typed as an opaque
  `map(string)`; its exact keys are only confirmed after a first apply, so
  `llm_project_endpoint` resolves defensively.

## Non-goals

Explicitly out of scope for Phase 16, deferred to later phases or rejected:

- RAG, vector storage and Azure AI Search;
- the FastAPI product (`products/enterprise-llm-demo`) and its provider adapter;
- agents, tool calling and agentic orchestration;
- an application-level LLMOps framework or evaluation dataset;
- multiple models, multiple providers, or any fine-tuning;
- Container Apps, Kubernetes or other application hosting;
- a DEV/STG/PROD Foundry rollout;
- private endpoints, private DNS and VNet injection for Foundry;
- Terraform-managed role assignments;
- resource groups, storage, Key Vault or Cosmos DB created by this capability;
- a self-hosted Azure DevOps agent.
