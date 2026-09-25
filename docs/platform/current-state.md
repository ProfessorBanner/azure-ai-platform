# Current Platform State

Repo-native checkpoint. Read this first in a new engineering session; it
describes the platform as it stands now, not how it got here. Anything not
stated here is either not built or not evidenced in this repository.

## Current phase

Phase 14.1 — Engineering Workflow Hardening.

## Last known-good source

- `main`: `661a9a93552b8fb72286473e451ee1d04035a179`
  ("Merged PR 29: Add governed PROD Databricks promotion") — the last governed,
  merged state.
- Working branch at time of writing: `fix/prod-promotion-runid-gate`, HEAD
  `45ce66551ad987ccb6d0a1ee0b8915dc80a76af2` ("Fix PROD promotion run ID gate"),
  one commit ahead of `main` and not yet merged.

## Environment model

- **SANDBOX** — experimentation / manual environment class, not a promotion
  stage. Disposable content, no downstream guarantees, no production
  credentials. Its Unity Catalog objects are not managed in Terraform.
- **DEV / STG / PROD** — governed environments. DEV is the first governed
  deployment environment.
- Promotion chain: `feature -> PR -> main -> DEV -> STG -> PROD`.
- **Same-SHA promotion**: STG is triggered only by DEV CD completion on `main`,
  PROD only by STG CD completion on `main`; each downstream pipeline re-verifies
  that its own `Build.SourceVersion` equals the triggering run's source commit
  and fails closed otherwise.
- One Terraform state per environment:
  `platform/{sandbox,dev,stg,prod}.tfstate`, with backend configs under
  `backend/`. Resource groups `rg-aiplatform-<env>` are externally provisioned
  and referenced by data source; Terraform never creates or destroys them.

## Infrastructure baseline

Established pattern, instantiated per environment from environment-neutral
modules in `infrastructure/modules/` (`network`, `storage`,
`storage_private_access`, `key-vault`, `log-analytics`, `databricks-workspace`,
`databricks-access-connector`):

- **VNet-injected Databricks** — Premium workspace bound to a platform-managed
  VNet (`vnet-aiplatform-<env>`), one workspace per environment,
  `prevent_destroy` on the workspace.
- **No public compute IPs** — Secure Cluster Connectivity (`no_public_ip = true`)
  fixed at workspace creation in every environment.
- **Delegated Databricks subnets** — `snet-dbx-host-<env>` and
  `snet-dbx-container-<env>`, both delegated to `Microsoft.Databricks/workspaces`.
- **NSGs / NAT** — one `nsg-dbx-<env>` associated with both Databricks subnets;
  a Standard NAT gateway (`nat-dbx-<env>` + `pip-dbx-nat-<env>`) associated with
  both subnets provides deterministic egress.
- **Private endpoint subnet** — `snet-private-endpoints-<env>`.
- **ADLS Gen2** — StorageV2 with hierarchical namespace, Standard/LRS, public
  network access disabled by default and network `default_action = Deny`;
  Shared Key disabled, so providers authenticate Storage with Entra ID
  (`storage_use_azuread = true`).
- **Blob/DFS private endpoints and private DNS** — `privatelink.blob` and
  `privatelink.dfs` zones linked to the environment VNet, with private endpoints
  wired in through DNS zone groups.
- **Access Connector managed identity** — one connector per environment with a
  system-assigned identity, granted Storage Blob Data Contributor on that
  environment's ADLS account.
- **Unity Catalog foundation** — in DEV, STG and PROD: an isolated storage
  credential (`sc-aiplatform-<env>`) backed by the connector identity, an
  external location (`el-aiplatform-<env>`) over `abfss://ucroot@…`, an isolated
  catalog named after the environment with a `platform_test` schema, and
  explicit workspace bindings for catalog, external location and credential.
  The `ucroot` filesystem is created through the ARM management plane (AzAPI),
  so the Terraform runner never needs Storage data-plane access.

Sandbox has the network, storage, private-access, Key Vault, Log Analytics,
workspace and Access Connector layers, but **no** Terraform-managed Unity
Catalog objects.

## Platform mode (idle / active)

Every environment root carries a committed `idle_mode` switch in a dedicated
file (`infrastructure/environments/<env>/platform_mode.tf`, default `false`). In IDLE
mode the NAT gateway, its subnet/public-IP associations and the two storage
private endpoints are removed to stop hourly networking charges; the NAT public
IP resource and address, VNet/subnets/NSG, private DNS zones and links, storage
and data, Key Vault, Log Analytics, workspace, Access Connector, Unity Catalog
objects and identities are all retained. The CD destructive-change guard reads
the mode from the plan and permits exactly those six addresses (delete-only)
and nothing else; the CI scripts refuse a `TF_VAR_idle_mode` override; and
`scripts/ci/check-platform-mode.sh` blocks every product CD deploy into an
idle environment.
Workload suspension/restoration (job schedules, apps) is scripted under
`scripts/idle/`. Procedure, inventory and residual costs:
`docs/runbooks/platform-idle-mode.md`; decision: ADR 0013.

## Identity model

Architectural separation — every identity is distinct, and no long-lived
credential is used anywhere:

| Purpose | Identity |
|---|---|
| Human / local access | Operator Entra ID; local Terraform plans use a Databricks CLI profile (`DATABRICKS_CONFIG_PROFILE`) |
| Terraform CI (plan, read-only, shared) | `sc-azure-terraform-plan` |
| Terraform apply, per environment | `sc-azure-terraform-apply-{sandbox,dev,stg,prod}` |
| Databricks CI | Dedicated workspace service principal, OIDC |
| DEV deployment | Dedicated DEV service principal, OIDC |
| STG deployment | Dedicated STG service principal, OIDC |
| PROD deployment | Dedicated PROD service principal, OIDC |

- **OIDC / WIF everywhere**: Azure work uses workload identity federation via
  Azure DevOps service connections; Databricks pipelines use
  `DATABRICKS_AUTH_TYPE: azure-devops-oidc` with the pipeline's
  `System.AccessToken`.
- Every Databricks pipeline verifies at runtime that the authenticated identity
  is exactly the expected client ID and fails otherwise.
- **No PATs, client secrets, SAS tokens or storage account keys** — an
  architectural standard, not a convention. Shared Key is disabled on the
  storage accounts; the Terraform backend uses Entra ID auth.
- Client IDs and workspace URLs are non-secret configuration and appear in the
  pipeline definitions; no secret material is stored in this repository.

## Delivery model

- **Databricks CI** (`azure-pipelines/databricks-ci.yml`): OIDC identity
  verification, then `ruff format --check`, `ruff check`, `mypy`, `pytest`, then
  `databricks bundle validate -t dev`.
- **Bundle-based governed deployment**: the `hello-databricks` Declarative
  Automation Bundle (`products/hello-databricks/`) with `dev`, `stg` and `prod`
  targets. Each target pins its workspace host, `run_as` service principal,
  catalog, schema and compute policy; `prod` uses `mode: production` with
  `git.branch: main`.
- **DEV** (`databricks-cd-dev.yml`): triggered by `main`; validate then deploy.
- **STG** (`databricks-cd-stg.yml`): no repo trigger — triggered only by DEV CD
  completion on `main`. Promotion gate → identity verification → validate → plan
  → deploy → run.
- **PROD** (`databricks-cd-prod.yml`): no repo trigger — triggered only by STG CD
  completion on `main`. Two stages: a cheap, checkout-free `promotion_gate`
  stage, then a `deploy_prod` deployment job bound to the **Azure DevOps
  Environment `aiplatform-prod`**, which carries the manual approval. Approvers
  are only asked about runs that already proved their provenance. The PROD job
  validates, plans, deploys, runs the job and verifies the output table.
- **Terraform delivery** is separate: one CI pipeline
  (`terraform-ci-platform.yml`, four plan-only stages) and one CD pipeline
  (`terraform-cd-platform.yml`) running sandbox independently and
  dev → stg → prod sequentially, each with a destructive-change guard,
  exact-plan apply and post-apply drift check; PROD apply is approval-gated.

## Current known-good capability

Phase 13 proves the promotion chain end to end: a single commit on `main` flows
DEV → STG → PROD by same-SHA promotion, deploying the same bundle into three
isolated, VNet-injected, private-endpoint-only workspaces, each with its own
Unity Catalog catalog and its own OIDC deployment identity, with PROD gated by
an Azure DevOps Environment approval and confirmed by reading back
`prod.hello_databricks.bundle_test_output`. Governed Databricks delivery to
production is therefore a working capability, not a design.

## Current work

Phase 14A workflow hardening. In flight on `fix/prod-promotion-runid-gate`: the
PROD promotion gate additionally rejects an empty or non-numeric triggering STG
run ID, so an unresolved pipeline-resource variable cannot pass the gate.

## Next acceptance gate

A repo-native current-state checkpoint exists and can be used by a fresh
engineering session without reconstructing platform state from chat history.

## Known exceptions / deferred work

Only exceptions supported by current repository documentation:

- **Databricks control-plane public network access remains enabled** on the
  workspaces (`public_network_access_enabled` defaults to `true`) — a documented
  interim posture in ADR 0005, not the intended production posture.
- **Owner and expiry tags are not emitted** by the common tag schema, although
  `CLAUDE.md` requires them; carried uniformly across environments (ADR 0005).
- **`prevent_destroy` also protects sandbox** workspace/resources, so disposing
  of sandbox platform substrate requires an explicit code change (ADR 0005).
- **`docs/architecture.md`, `README.md` and `docs/implementation-backlog.md` are
  stale** — they still describe "Stage 6: dev Azure platform foundation" and
  predate the Databricks, networking, Unity Catalog and promotion work. Prefer
  this document and the ADRs.
- **`azure-pipelines/databricks-dev-cd.yml` duplicates
  `azure-pipelines/databricks-cd-dev.yml`** — both are `main`-triggered DEV
  bundle pipelines; which one is registered in Azure DevOps is not established
  from the repository.
- **Azure DevOps objects are configured outside this repository** — Environment
  approvals, service connections and pipeline registrations (including the
  `aiplatform-prod` approval) are not codified here.
