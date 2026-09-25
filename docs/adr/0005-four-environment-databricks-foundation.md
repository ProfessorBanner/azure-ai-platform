# ADR 0005: Four-environment Azure / Databricks foundation

## Status

Accepted

## Context

ADR 0004 established the first dev platform substrate (storage, Key Vault, Log
Analytics) under an independent state key `platform/dev.tfstate`, in the
externally provisioned `rg-aiplatform-dev`. That DEV foundation is now
**deployed**.

Phase 7 grows this into a **four-environment** foundation — `sandbox`, `dev`,
`stg`, `prod` — and adds Azure Databricks (a Premium workspace and an Access
Connector) to every environment. A first-class constraint is that all reusable
structure (modules, variable names, naming, pipelines) is authored so the
environments stand up by instantiation and configuration alone, with no redesign.

Unity Catalog, the Databricks Terraform provider, storage credentials, external
locations, service principals, workloads/bundles, jobs, clusters, notebooks,
model serving/registry and private networking are explicitly **out of scope** for
Phase 7 — they are Phase 8.

## Decision

### Sandbox is an environment *class*, not a promotion stage

`sandbox` is a separate environment class for human experimentation, interactive
development, temporary libraries and disposable data. It has weaker durability,
no downstream guarantees and no production credentials, and it is **NOT** part of
the governed promotion chain. Useful sandbox work is committed to Git and then
flows through the governed lifecycle:

```
feature branch -> PR -> main -> DEV -> STG -> PROD
```

DEV is the **first governed deployment environment**.

### One environment resource model, five resources each

Every environment (sandbox/dev/stg/prod) contains the same resource model:

- ADLS Gen2 StorageV2 account
- Key Vault
- Log Analytics workspace
- Premium Azure Databricks workspace
- Azure Databricks Access Connector (system-assigned managed identity)

One workspace and one Access Connector **per environment** — never shared. This
preserves per-environment blast-radius, governance and identity isolation, and
means the Phase 8 storage RBAC grant for each connector is naturally scoped per
environment.

For DEV the first three already exist and are preserved in place (see below);
Phase 7 only adds the two Databricks resources there.

### Reusable modules are environment-neutral

Modules under `infrastructure/modules/` (`storage`, `key-vault`, `log-analytics`,
`databricks-workspace`, `databricks-access-connector`) contain **no**
environment-specific names. Environment differences — physical names, cost caps,
retention, tags — are configuration values supplied by the environment roots.
There are **no** sandbox-specific module forks; sandbox differs only by the
values it passes (e.g. shorter storage soft-delete, a smaller Log Analytics daily
cap, a `lifecycle = disposable` tag).

### One Terraform state per environment

Consistent with ADR 0002/0004, each environment has a single independent state
key in the shared backend container, authenticated with Microsoft Entra ID
(`use_azuread_auth = true`; no storage key):

| Environment | State key                 | Backend config                   |
|-------------|---------------------------|----------------------------------|
| sandbox     | `platform/sandbox.tfstate`| `backend/platform-sandbox.hcl`   |
| dev         | `platform/dev.tfstate`    | `backend/platform-dev.hcl`       |
| stg         | `platform/stg.tfstate`    | `backend/platform-stg.hcl`       |
| prod        | `platform/prod.tfstate`   | `backend/platform-prod.hcl`      |

Environment resource groups (`rg-aiplatform-{sandbox,dev,stg,prod}`) are
externally provisioned and referenced via `data.azurerm_resource_group`.
Terraform never creates or destroys an environment resource group.

### Preserving the deployed DEV state

DEV is live under `platform/dev.tfstate`. Phase 7 **only adds** the two
Databricks modules to the DEV root and adds the missing root outputs. The
existing `module.storage`, `module.key_vault` and `module.log_analytics` blocks —
and therefore their Terraform resource addresses — are **unchanged**: no rename,
re-nesting or re-keying. Structural symmetry across environments was never
pursued at the cost of DEV state stability. The expected DEV plan is **2 to add,
0 to destroy** (plus new outputs, including `storage_account_name =
"stexampledevdata"`, which do not imply any resource recreation). No second
storage account, Key Vault or Log Analytics workspace is created; nothing is
replaced or moved between states.

### Stable environment-root output contract

Every root exposes the SAME output interface, sourced from modules/resources and
never hard-coding physical Azure names:

```
resource_group_name
storage_account_id, storage_account_name, storage_primary_dfs_endpoint
key_vault_id, key_vault_uri
log_analytics_workspace_id
databricks_workspace_id, databricks_workspace_name, databricks_workspace_url
databricks_access_connector_id, databricks_access_connector_name,
databricks_access_connector_principal_id
```

Phase 8 consumes infrastructure through these outputs without knowing Azure
naming conventions.

### Databricks scope and the Access Connector role-assignment boundary

The workspace is Premium, UK South. No VNet injection, no Private Link, no
customer-managed keys; the control plane keeps public network access enabled as
an interim posture. `prevent_destroy` guards the workspace.

**Deliberate decision — Secure Cluster Connectivity (`no_public_ip = true`).**
Although the Phase 7 brief only required "no VNet injection / no Private Link",
this ADR additionally adopts Secure Cluster Connectivity on the Databricks-managed
VNet as an explicit Phase 7 decision: compute nodes get no public IPs, which is
strictly better than the provider default without needing VNet injection or
private endpoints. It is set **at creation on every environment** because
changing `no_public_ip` later forces a workspace replacement — which the governed
environments must never incur. `prevent_destroy` is likewise fixed on the
workspace in all environments (it must be a literal; sandbox platform substrate
is therefore durable even though sandbox *content* is disposable).

The Access Connector is created with a system-assigned managed identity but
receives **no Azure role assignment in Phase 7**. This is deliberate, not
unfinished: the connector is an Azure identity primitive whose storage RBAC is
consumed only by a Unity Catalog storage credential / external location, which do
not exist until Phase 8. Granting storage access now would give standing
permissions to an unused identity, so identity creation and permission grant are
kept as separate, independently reviewed changes.

Terraform manages **durable platform/governance** resources; application
**workloads** (jobs, notebooks, pipelines) are deployed later via Databricks
Declarative Automation Bundles. The Databricks Terraform provider is not
introduced in Phase 7.

### Pipelines: one CI architecture, one CD architecture

The bootstrap pipelines are untouched. The platform pipelines are extended to
four environments without creating four pipeline families:

- **CI** (`terraform-ci-platform.yml`): one pipeline, four independent stages
  (sandbox/dev/stg/prod), each validating and planning its environment with the
  shared **plan-only** identity `sc-azure-terraform-plan`, differing only by
  working directory, backend config, TF_VAR inputs and artifact name. These are
  **advisory preview plans** for PR review — never deployment plans, never apply.
- **CD** (`terraform-cd-platform.yml`): one pipeline instantiating a shared
  per-environment stage template. Sandbox runs an **independent** plan→apply→
  validate path. The governed path runs **dev → stg → prod** sequentially
  (`stg` after `apply_dev`, `prod` after `apply_stg`), with **prod apply gated by
  a manual approval**. Each environment produces its own binary plan, exposes a
  sanitized plan text, runs the destructive-change guard (`terraform show -json`),
  applies the **exact** reviewed binary plan, and runs a post-apply drift check
  (`terraform plan -detailed-exitcode`), with environment-specific artifact names.

The CD preview plans (CI) and the CD deployment plans are distinct artifacts:
CD re-plans each environment from the merged commit and applies that exact binary
plan; it never promotes a PR-time plan.

### Identity model (WIF only)

| Purpose | Identity |
|---------|----------|
| Plan (read-only, shared) | `sc-azure-terraform-plan` |
| Apply sandbox | `sc-azure-terraform-apply-sandbox` |
| Apply dev | `sc-azure-terraform-apply-dev` |
| Apply stg | `sc-azure-terraform-apply-stg` |
| Apply prod | `sc-azure-terraform-apply-prod` |

Azure DevOps Environments `azure-ai-platform-{sandbox,dev,stg,prod}` provide the
deployment gates. The shared plan identity generates every plan; each apply
identity only consumes its environment's approved binary plan, scoped
(Contributor) to its own resource group. No client secrets, PATs, SAS tokens or
storage keys anywhere.

**Apply approval policy.** `azure-ai-platform-prod` **MUST require a manual
approval** — this is the human gate on production. `sandbox`, `dev` and `stg`
**apply automatically** once their guards pass (validate → plan →
destructive-change guard → exact-plan apply → drift check); requiring a manual
click for each would add friction without adding safety, since the reviewed merge
to `main` is already the human authorisation and the guards prevent unexpected
destructive change. This resolves the apparent tension with the blanket
"`terraform apply` requires approval" rule in `CLAUDE.md`: Claude never runs apply
itself, and in CD the human control is PROD's Environment approval, backed by the
per-environment guards. Lower-environment approvals remain policy-dependent and
can be tightened later without code changes.

### Deliberate deferral: networking

Phase 7 optimises for a minimum-viable, reproducible platform foundation and
learning velocity, **not** production network isolation. VNet injection, Private
Link, private endpoints and private DNS are consciously deferred (recorded here
as known technical debt, not an accidental omission). Phase 7 delivers Azure
resource, identity, state and deployment isolation; Databricks logical governance
and data/network security follow in Phase 8+.

## Consequences

### Positive

- Four isolated environments with per-environment state, identity and apply
  blast-radius; sandbox cleanly separated from the governed chain.
- Reusable, environment-neutral modules; new environments are instantiation +
  configuration, with no module or pipeline redesign.
- Deployed DEV state is preserved (address-stable); expected DEV plan is 2 adds.
- Stable output contract lets Phase 8 consume infrastructure abstractly.
- No secrets, PATs, role assignments or persistent compute introduced; the
  connector identity exists but is inert until Phase 8.

### Negative

- Storage and Key Vault names are globally unique and must be chosen/confirmed
  per environment before apply.
- The workspace control plane keeps public network access enabled as an interim
  posture; SCC (`no_public_ip`) is fixed at creation because toggling it later
  forces workspace replacement.
- `prevent_destroy` in the shared modules also protects sandbox resources, so
  fully disposing a sandbox resource requires an explicit code change.
- Additional Azure DevOps objects (the four per-environment apply service
  connections and Environment approvals) must be created/authorised manually.
- The owner/expiry tags named in `CLAUDE.md` are not yet emitted by the common
  tag schema (a pre-existing gap carried forward uniformly across environments;
  tracked for a later tagging pass).
