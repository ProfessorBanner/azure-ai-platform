# Architecture

This document describes the **current state** of the platform and is kept in
sync as later backlog stages land. It is not aspirational: where a capability is
planned but not yet built, it is marked as such.

The platform is at **Stage 6: dev Azure platform foundation**. Stages 1–5
(engineering foundation) are complete; the only provisioned Azure resource so
far is the Terraform state backend (see
`docs/adr/0002-terraform-state-backend.md`), and no application-facing Azure or
Databricks resources exist yet. Stage 6 begins building the dev Azure platform
foundation.

## Engineering platform direction

Azure DevOps is the primary, enterprise-aligned engineering platform because it
matches the target enterprise stack (Azure + Azure DevOps + Azure Databricks).
See `docs/adr/0003-azure-devops-primary-cicd.md`.

- **Azure DevOps** — primary engineering platform.
- **Azure Repos** — the canonical repository.
- **Azure Pipelines** — owns CI/CD. A plan-only Terraform CI pipeline exists
  (backlog TFCI-1) and a controlled Terraform CD/apply pipeline exists
  (backlog TFCD-2), gated by an Azure DevOps Environment approval.
- **GitHub** — retained only as a temporary backup remote; owns no deployment.

## Source-control topology

The migration to Azure Repos is complete. The repository has two remotes:

| Remote | URL | Role |
|--------|-----|------|
| `azure` / `origin` | `https://dev.azure.com/example-org/azure-ai-platform/_git/azure-ai-platform` | Canonical repository |
| `github` | `https://github.com/example-user/azure-ai-platform.git` | Temporary backup mirror (no deployment) |

- Azure DevOps organisation: `example-org`
  (`https://dev.azure.com/example-org`).
- Project: `azure-ai-platform`; repository: `azure-ai-platform`.

Migration coordinates, authentication and branch policy are documented in
`docs/runbooks/azure-repos-migration.md`.

## Branch protection and change flow

`main` is the protected default branch:

- All changes to `main` require a **pull request**; direct pushes are blocked.
- **Policy bypass and force-push are prohibited** for non-administrators.
- At least one reviewer is required (licence permitting), self-approval
  disallowed, comment resolution required.
- A **build validation** policy slot is reserved for the Terraform CI pipeline.
  The pipeline now exists (`azure-pipelines/terraform-ci.yml`, TFCI-1); the
  policy is attached in Azure DevOps as a separate manual step (backlog TFCI-2),
  documented in `docs/runbooks/terraform-ci.md`.

**Claude Code** operates locally against the same Git repository: it creates
feature branches and pushes them only with explicit human approval, never to
`main`, never force-pushing, never bypassing policy. PR completion is a human,
policy-gated action.

## Identity and authentication (target)

Automated Azure and Databricks authentication will use Microsoft Entra
**workload identity federation** exclusively once Azure Pipelines is introduced
(backlog Stage 4). The controlling rules:

- **Workload identity federation is mandatory** for Azure Pipelines.
- **No PAT or client secret** may be used for pipeline deployment; no storage
  account keys; no Databricks personal access tokens.
- Human Git access authenticates via Microsoft Entra ID (Git Credential
  Manager), not a PAT.
- Separate, least-privilege `plan` and `apply` service connections will be used;
  neither is granted Owner or User Access Administrator, nor subscription-wide
  Contributor.

## Terraform state backend (current)

Terraform state is stored in a dedicated Azure Storage account using the
`azurerm` backend with Microsoft Entra authentication (`use_azuread_auth =
true`), container-scoped Blob Data Contributor access, TLS 1.2 minimum and
HTTPS-only traffic. State keys are separated per component and environment. Full
detail, hardening status and recovery are in
`docs/adr/0002-terraform-state-backend.md` and
`docs/runbooks/terraform-state-recovery.md`.

## Terraform CI pipeline (current)

`azure-pipelines/terraform-ci.yml` is a **plan-only** pipeline (backlog TFCI-1)
that runs on feature branches and, via branch-policy build validation, on pull
requests to `main`. On a Microsoft-hosted Ubuntu agent it installs pinned
Terraform/TFLint/Trivy, then runs `fmt-check`, `init` (remote `azurerm`
backend, Entra auth), `validate`, `tflint`, a Trivy scan and `terraform plan`,
publishing only a redacted `plan.txt` and validation logs.

- **Identity:** the plan-only service connection `sc-azure-terraform-plan` — a
  user-assigned managed identity with **workload identity federation**. It holds
  only **Reader** (subscription) and **Storage Blob Data Contributor** (tfstate
  container), so it can read infrastructure and state but cannot mutate either.
- **No long-lived credentials:** a fresh OIDC token is requested per run
  (`AzureCLI@2` + `addSpnToEnvironment`) and mapped to transient `ARM_*`
  variables (`ARM_USE_OIDC`, `ARM_USE_AZUREAD`, `ARM_CLIENT_ID`, `ARM_TENANT_ID`,
  `ARM_SUBSCRIPTION_ID`, `ARM_OIDC_TOKEN`). No PAT, client secret, storage key or
  SAS token is used; the token is never logged.
- **Apply is impossible here:** there is no `apply`/`destroy` step and the plan
  identity lacks any write role. Apply is a separate, human-approved CD pipeline
  (backlog Stage 6).

Operational detail — service-connection permissions, publishing rules, and how
the PR build-validation policy is attached — is in
`docs/runbooks/terraform-ci.md`.

## Terraform CD pipeline (current)

`azure-pipelines/terraform-cd.yml` is a **controlled apply** pipeline (backlog
TFCD-2) that runs **only on merge to `main`** (no PR trigger, no feature-branch
trigger). It deploys to **dev only**. At the current stage the expected apply is
a **no-op**: the pipeline exists to prove the controlled CD path (post-merge
plan → approval → exact-plan apply → drift check), not to change infrastructure.

Flow, across two stages on Microsoft-hosted Ubuntu agents:

1. **Validate & plan** (identity `sc-azure-terraform-plan`): `fmt-check`,
   `tflint`, Trivy, then `init`/`validate`/`terraform plan -out` from the
   **exact merged `main` commit**. It publishes a redacted human-readable
   `plan.txt` + logs, and a lightweight guard fails the run on any unexpected
   **destructive** change (delete/replace, inspected via `terraform show -json`).
2. **Approve & apply** (identity `sc-azure-terraform-apply`): a **deployment
   job** bound to the Azure DevOps Environment `azure-ai-platform-dev`, whose
   **manual approval** is the human gate. After approval it applies the **exact
   saved binary plan** (never re-planning) and runs
   `terraform plan -detailed-exitcode` to confirm **no drift** (exit 0).

- **Two separate identities, never mixed.** The **plan** connection holds only
  **Reader** (subscription) + **Storage Blob Data Contributor** (tfstate
  container) and cannot mutate anything. The **apply** connection holds
  **Contributor on `rg-aiplatform-bootstrap-dev` only** + Storage Blob Data
  Contributor (tfstate container) — never Owner, User Access Administrator, or
  subscription-wide Contributor. The apply scope is the blast-radius control.
- **Exact-plan promotion.** The binary plan is passed to the apply stage only as
  a short-lived internal pipeline artifact (`terraform-plan-binary`); the plan a
  human approves is the plan that applies.
- **No long-lived credentials.** Both stages request a fresh OIDC token and set
  only transient `ARM_*` variables; no storage key, SAS token, client secret or
  PAT is used. The backend authenticates to state via Entra
  (`use_azuread_auth = true`). No `terraform destroy` is ever run.
- **Never published:** Terraform state, `.terraform/`, OIDC tokens, Azure CLI
  credentials or environment dumps.

Operational detail — identity separation, exact-plan promotion, the approval
gate, the destructive-change guard, rollback/state recovery, and the remaining
manual Azure DevOps configuration — is in `docs/runbooks/terraform-cd.md`.

The controlled-apply flow is now defined once in the shared stage template
`azure-pipelines/templates/terraform-cd-stages.yml` and consumed by both the
bootstrap wrapper (`terraform-cd.yml`) and the platform wrapper
(`terraform-cd-platform.yml`); the plan-only step sequence is likewise shared
via `templates/terraform-checks.yml` (used by `terraform-ci.yml` and
`terraform-ci-platform.yml`). Each wrapper owns only its trigger and its
component variables (working directory, backend config, and the `TF_VAR_*`
names). Identity separation, exact-plan promotion, the approval gate and the
scripts under `scripts/ci/` are unchanged and shared.

## Dev platform foundation (current)

Stage 6 introduces the first application-facing Azure resources, authored under
`infrastructure/environments/dev/` as pure Terraform. **The configuration is
authored but not yet applied.** It uses a **new, independent** state key
`platform/dev.tfstate` (`backend/platform-dev.hcl`) in the same backend
container as bootstrap and authenticates to state with Microsoft Entra ID
(`use_azuread_auth = true`).

The root composes three reusable modules (`infrastructure/modules/`), each
tagged with the platform schema (`environment`, `platform`, `managed_by`,
`purpose`), in region UK South:

- **Storage** — an ADLS Gen2 account (StorageV2 + hierarchical namespace,
  Standard LRS) intended as the Data Lake / future Databricks account. TLS 1.2
  minimum, HTTPS-only, no public blob/container access, **Shared Key disabled**
  (Entra only), and **data-plane public network access default-denied**. No
  containers are created yet. Blob versioning is unavailable with a hierarchical
  namespace, so data protection is blob/container soft delete. `prevent_destroy`
  is set.
- **Key Vault** — Azure RBAC authorization (no access policies), purge
  protection enabled, soft delete retained, no secrets. Public network access is
  **temporarily enabled** as a documented dev lab posture (tracked Trivy
  deviation `AVD-AZU-0013`, review 2026-11-30). `prevent_destroy` is set.
- **Log Analytics** — a PerGB2018 workspace with short retention and a daily
  ingestion cap; no diagnostic settings attached yet.

### Resource group and state ownership

The environment resource group `rg-aiplatform-dev` is **externally provisioned**
(out-of-band) and referenced via `data.azurerm_resource_group`; Terraform never
creates or destroys it. Ownership boundaries:

| State key | Owns | Must not touch |
|-----------|------|----------------|
| `bootstrap.tfstate` | The state backend (`rg-aiplatform-bootstrap-dev`, tfstate account + container) | Platform resources |
| `platform/dev.tfstate` | Storage, Key Vault, Log Analytics in `rg-aiplatform-dev` | The bootstrap backend; the `rg-aiplatform-dev` group itself |
| *external* | The `rg-aiplatform-dev` resource group | — |

Foundry, Container Apps, Cosmos DB, AI Search and application resources are
deferred. Rationale is in `docs/adr/0004-dev-platform-foundation.md`.

## Four-environment Azure / Databricks foundation (current)

Phase 7 extends the platform to **four environments** — `sandbox`, `dev`, `stg`,
`prod` — and adds Azure Databricks to each. The DEV foundation above is
**deployed**; Phase 7 adds its Databricks resources in place.

**Sandbox is a separate environment class**, not a promotion stage: human
experimentation, interactive development, temporary libraries, disposable data,
weaker durability, no downstream guarantees, no production credentials. Useful
sandbox work is committed to Git and then flows through the governed lifecycle.
**DEV is the first governed deployment environment**:
`feature -> PR -> main -> DEV -> STG -> PROD`.

Every environment root (`infrastructure/environments/<env>/`) composes the SAME
reusable, environment-neutral modules — `storage`, `key-vault`, `log-analytics`,
`databricks-workspace`, `databricks-access-connector` — into an externally
provisioned resource group referenced by data source. Each has:

- **one Premium Databricks workspace** (UK South, Secure Cluster Connectivity /
  `no_public_ip`, no VNet injection, no Private Link, no CMK, `prevent_destroy`);
- **one Databricks Access Connector** with a system-assigned managed identity and
  **no role assignment** in Phase 7 (the identity is inert until the Phase 8
  Unity Catalog storage grant consumes it);
- **one independent Terraform state** and a **stable output contract** shared by
  all roots (storage/Key Vault/Log Analytics IDs + names, and the Databricks
  workspace/connector IDs, names, URL and connector principal ID), sourced from
  modules — never hard-coded physical names — so Phase 8 consumes infrastructure
  abstractly.

Modules are never forked for sandbox; sandbox differs only by configuration
(shorter storage soft-delete, a smaller Log Analytics daily cap, a
`lifecycle = disposable` tag). Terraform manages durable platform/governance
resources; workloads are deployed later via Declarative Automation Bundles. The
Databricks Terraform provider, Unity Catalog and private networking are Phase 8.

### State ownership boundaries (four environments)

| State key | Owns | Must not touch |
|-----------|------|----------------|
| `bootstrap.tfstate` | The state backend (`rg-aiplatform-bootstrap-dev`, tfstate account + container) | Platform resources |
| `platform/sandbox.tfstate` | Storage, Key Vault, Log Analytics, Databricks workspace + Access Connector in `rg-aiplatform-sandbox` | The bootstrap backend; the resource group itself; other environments |
| `platform/dev.tfstate` | The same five resources in `rg-aiplatform-dev` (Databricks added in place; storage/KV/LA preserved) | The bootstrap backend; the resource group itself; other environments |
| `platform/stg.tfstate` | The same five resources in `rg-aiplatform-stg` | The bootstrap backend; the resource group itself; other environments |
| `platform/prod.tfstate` | The same five resources in `rg-aiplatform-prod` | The bootstrap backend; the resource group itself; other environments |
| *external* | The `rg-aiplatform-{sandbox,dev,stg,prod}` resource groups | — |

### Pipelines (four environments)

One platform CI architecture and one platform CD architecture (not four
families). CI (`terraform-ci-platform.yml`) validates + plans each environment
independently with the shared read-only plan identity `sc-azure-terraform-plan` —
advisory preview plans, never apply. CD (`terraform-cd-platform.yml`) runs an
independent sandbox path (plan → apply → validate) and a governed
`dev -> stg -> prod` chain, each with an independent binary plan, sanitized plan
text, destructive-change guard (`terraform show -json`), exact-plan apply and a
post-apply drift check (`terraform plan -detailed-exitcode`), using
environment-specific apply identities `sc-azure-terraform-apply-<env>`. PROD
requires a manual approval on its Environment; sandbox/dev/stg apply
automatically after their guards pass (the reviewed merge to `main` is the human
authorisation).

Rationale, the stable output contract and the Phase 8 boundary are in
`docs/adr/0005-four-environment-databricks-foundation.md`.

## Forward plan

The staged roadmap — Azure landing zone, security baseline, Azure DevOps
foundation, workload identity federation, Terraform CI/CD, Databricks
provisioning and the first agentic POC — is tracked in
`docs/implementation-backlog.md`.
