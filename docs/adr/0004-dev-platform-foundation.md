# ADR 0004: Dev Azure platform foundation

## Status

Accepted

## Context

Stages 1–5 built the engineering foundation: Azure Repos as the canonical
repository, Azure Pipelines CI/CD on workload identity federation with separate
plan and apply identities, a Terraform state backend, and a controlled
plan/apply flow gated by an Azure DevOps Environment approval. No
application-facing Azure resources existed yet.

Stage 6 introduces the first real dev platform resources. The design goal is the
smallest viable, secure and reproducible foundation that later stages
(Databricks, Unity Catalog, agentic workloads) can build on, without pulling
those later concerns forward.

## Decision

Provision a minimal dev platform as pure Terraform under a **new, independent**
state, composed of three building blocks only:

1. an ADLS Gen2 storage account (StorageV2 + hierarchical namespace), intended
   as the Data Lake / future Databricks storage account;
2. an Azure Key Vault (Azure RBAC authorization, purge protection);
3. a Log Analytics workspace (PerGB2018, short retention, daily ingestion cap).

Everything is authored as reusable modules under `infrastructure/modules/` and
instantiated for `dev` in `infrastructure/environments/dev/`. Default region is
UK South. No role assignments, no secrets, and no persistent compute are created
in this change.

### Why bootstrap and platform state are separated

The bootstrap configuration (`bootstrap/`, state key `bootstrap.tfstate`) owns
the state backend itself — the storage account and container that hold all other
state. Mixing the platform resources into that state would make the component
that *stores* state depend on the component that *is stored*, and would widen the
blast radius of every platform change to the state backend.

The platform therefore uses a **separate, independent** state key,
`platform/dev.tfstate`, in the *same* backend container
(`backend/platform-dev.hcl`). Separation gives:

- independent plan/apply lifecycles and locks per component;
- a smaller blast radius (a platform mistake cannot corrupt bootstrap state);
- clear ownership boundaries (see below);
- alignment with the forward state-key convention already recorded in ADR 0002.

The bootstrap state and its backend resources are **not touched** by this stage.

### Why `rg-aiplatform-dev` is externally bootstrapped

The environment resource group `rg-aiplatform-dev` is provisioned out-of-band
(externally bootstrapped) and is **referenced by a data source**, never created
or destroyed by this configuration. Reasons:

- **Lifecycle independence.** The resource group is a long-lived container whose
  existence should not be coupled to, or endangered by, routine platform applies
  (or a mistaken destroy).
- **Least privilege / separation of duties.** Creating a resource group is a
  higher-privilege act than placing resources into an existing one. Keeping RG
  creation outside this state lets the platform apply identity remain scoped to
  operating *within* the group rather than managing the group's own lifecycle.
- **Blast-radius control.** Terraform cannot delete or recreate the group, so an
  errant plan cannot take the whole environment (and anything else placed in the
  group) with it.

Consequently, `azurerm_resource_group` is intentionally absent from this
configuration; `data.azurerm_resource_group.platform` supplies the name and the
apply fails fast (rather than creating anything) if the group is missing.

### Why the initial platform is only storage, Key Vault and Log Analytics

These three are the minimum durable substrate that later work needs:

- **Storage (ADLS Gen2)** is the data foundation for analytics and Databricks.
- **Key Vault** is the governed secret store required before any workload holds
  a secret.
- **Log Analytics** is the observability sink required before diagnostics,
  agent telemetry, or cost/anomaly signals can land anywhere.

They are cheap, scale-to-zero or consumption-priced, carry no persistent
compute, and have no interdependencies to untangle later. Starting here keeps
the platform "smallest viable" while unblocking the next stages.

### Why networking and Databricks are deferred

- **Networking (VNet, subnets, private endpoints, private DNS)** is deliberately
  out of scope. A private-endpoint design needs known CI/workstation egress and
  a DNS strategy; doing it now would be speculative and would block progress. As
  an interim posture, the **storage account is default-denied on the data plane**
  (public network access disabled, `network_rules` default `Deny`) because
  Terraform creates no data-plane objects in it, while the **Key Vault keeps
  public access temporarily enabled** so CI agents and the operator workstation
  can reach it without a private endpoint. The Key Vault posture is a documented,
  time-boxed lab deviation (Trivy `AVD-AZU-0013`, review by 2026-11-30), not the
  intended production posture.
- **Databricks / Unity Catalog** is deferred to its own stage. It is a larger,
  governed subsystem (workspace, access connector/managed identity, metastore,
  catalog grants) that deserves its own ADR and state boundary, and it depends on
  this storage foundation existing first.

Foundry, Container Apps, Cosmos DB, AI Search and application resources are
likewise out of scope for this stage.

### State ownership boundaries

| State key | Owns | Must not touch |
|-----------|------|----------------|
| `bootstrap.tfstate` | The state backend: `rg-aiplatform-bootstrap-dev`, the tfstate storage account and container | Platform resources |
| `platform/dev.tfstate` | Storage, Key Vault, Log Analytics inside `rg-aiplatform-dev` | The bootstrap backend; the `rg-aiplatform-dev` group itself (data-source only) |
| *external (out-of-band)* | The `rg-aiplatform-dev` resource group | — |

Both state keys live in the same backend container and authenticate to it with
Microsoft Entra ID (`use_azuread_auth = true`); neither uses a storage account
key. Future components (Databricks, applications) will take their own keys under
the same convention (ADR 0002).

## Consequences

### Positive

- Smallest viable, reproducible dev foundation with clear boundaries.
- Independent lifecycles and reduced blast radius per component.
- No new identities, secrets, role assignments or persistent compute introduced.
- Storage is locked down on the data plane from day one.

### Negative

- A globally unique storage account name and Key Vault name must be chosen and
  confirmed before apply.
- The Key Vault's temporary public network posture is a tracked deviation that
  must be revisited when networking lands.
- The platform depends on `rg-aiplatform-dev` being created out-of-band; a
  missing group fails the plan until it is provisioned.
- A second CD pipeline object (platform) must be created/authorised in Azure
  DevOps, and its Environment approval confirmed, as manual steps.
