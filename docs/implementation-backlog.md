# Implementation Backlog

This backlog turns the platform roadmap (see `docs/adr/0001-platform-scope.md`
and `docs/adr/0003-azure-devops-primary-cicd.md`) into an ordered sequence of
engineering tasks. Tasks are grouped into stages and are meant to be executed
in order — later tasks depend on earlier ones unless noted otherwise.

**Platform direction.** Azure DevOps is the primary source-control and CI/CD
platform because it matches the target enterprise stack (Azure + Azure DevOps +
Azure Databricks). Azure Repos is the canonical repository; Azure Pipelines
owns CI/CD; Azure DevOps Environments provide deployment approvals. GitHub is
deferred to an **optional** comparison stage (Stage 11) and is never allowed to
own deployment to an Azure DevOps-owned environment. See ADR 0003.

No stage may begin until the prior stage's Definition of Done is met, and no
`terraform apply`, `databricks bundle deploy`, `az role assignment create`,
`az ad app create`, `git push`, or `az devops`/service-connection creation
command may run without explicit human approval, per `CLAUDE.md`.

**High-level progress.** The engineering foundation (source control on Azure
Repos, Azure Pipelines CI/CD on workload identity federation with separate
plan/apply identities, the Terraform state backend, and the plan-only CI /
controlled-apply CD flow gated by the `azure-ai-platform-dev` Environment
approval) is in place. The **dev Azure platform foundation** is now authored as
code but not yet applied: a new independent state `platform/dev.tfstate`
(`infrastructure/environments/dev/`) provisions an ADLS Gen2 storage account, a
Key Vault and a Log Analytics workspace into the externally provisioned
`rg-aiplatform-dev` (referenced by data source). This realises the substance of
AZ-1 (tagging), AZ-3 (Key Vault) and the observability baseline, with two
deliberate deviations recorded in `docs/adr/0004-dev-platform-foundation.md`:
the environment resource group is **externally bootstrapped** (not created by
Terraform), and storage + Log Analytics are added alongside Key Vault as the
minimal foundation. Networking, Databricks and application resources remain
deferred.

**Phase 7 (four-environment Azure / Databricks foundation).** The platform now
spans **four environments** — `sandbox`, `dev`, `stg`, `prod` — each with its own
independent state (`platform/<env>.tfstate`), its own externally provisioned
resource group (referenced by data source), and the same reusable,
environment-neutral modules. Phase 7 adds a Premium Azure Databricks workspace
and a Databricks Access Connector (system-assigned managed identity, **no role
assignment yet**) to every environment. **Sandbox is a separate environment
class**, not a promotion stage; the governed chain is
`feature -> PR -> main -> DEV -> STG -> PROD`, with DEV the first governed
deployment environment. DEV is already deployed, so Phase 7 only **adds** its two
Databricks resources (expected plan: 2 add, 0 destroy) while preserving the
existing storage/Key Vault/Log Analytics addresses. CI/CD keeps one platform CI
and one platform CD architecture, with a shared read-only plan identity and
per-environment apply identities (`sc-azure-terraform-apply-<env>`) and
Environment approvals (prod mandatory). Unity Catalog, the Databricks Terraform
provider, storage credentials/external locations, service principals, Bundles and
private networking are the **Phase 8** boundary. See
`docs/adr/0005-four-environment-databricks-foundation.md`.

Task IDs are stable identifiers for cross-referencing in commits, PRs and ADRs
(e.g. `git commit -m "ADO-2: migrate repository into Azure Repos"`).

**Non-negotiable authentication rule (all stages):** automated Azure and
Databricks deployment uses Microsoft Entra **workload identity federation**
only. No client secrets, no storage account keys, no Databricks personal
access tokens are created for automation.

---

## Stage 0 — Bootstrap

Goal: a working local engineering environment and a Terraform state backend,
with no application-facing Azure resources yet. **(Implemented / in progress.)**

### BOOT-1: Author foundational documentation

- **Objective**: Real `README.md` and `docs/architecture.md` describing the
  platform's purpose, prerequisites, and day-to-day `make` workflow.
- **Dependencies**: None.
- **Implementation steps**:
  1. Write `README.md`: mission, prerequisites (`scripts/check-tools.sh`),
     quickstart (`make check-tools`, `make check`), layout, ADR links.
  2. Write `docs/architecture.md`: target-state description of tenant,
     subscription, resource groups, Databricks workspace, and the Azure
     DevOps CI/CD flow — kept in sync as later stages land.
- **Expected files**: `README.md`, `docs/architecture.md`.
- **Azure DevOps entity involved**: None (local documentation).
- **Azure / Databricks identity and permissions**: None.
- **Terraform state key affected**: None.
- **Validation commands**: `pre-commit run --files README.md docs/architecture.md`
- **Security implications**: Documentation only; use placeholders for
  subscription/tenant IDs and resource names.
- **Cost implications**: None.
- **Rollback procedure**: Revert the docs commit; no external state.
- **Definition of done**: Both files render, describe current (not
  aspirational) state, and pass pre-commit.

### BOOT-2: Python project scaffold

- **Objective**: Establish the Python toolchain (`uv`, Ruff, mypy, pytest)
  ahead of any agent code.
- **Dependencies**: None.
- **Implementation steps**:
  1. `uv init` a Python 3.12 project (`src/` layout).
  2. Configure `pyproject.toml`: Ruff (lint + format, `S`/bandit enabled),
     mypy (strict for production), pytest with `unit/`, `integration/`,
     `evaluation/` separation.
  3. Add `ruff-pre-commit` and a local `mypy` hook to
     `.pre-commit-config.yaml`; add a placeholder unit test.
- **Expected files**: `pyproject.toml`, `uv.lock`, `tests/unit/`,
  `.pre-commit-config.yaml` (updated).
- **Azure DevOps entity involved**: None.
- **Azure / Databricks identity and permissions**: None.
- **Terraform state key affected**: None.
- **Validation commands**: `make fmt-check && make lint && make test`
- **Security implications**: Pin dependencies via committed `uv.lock`; enable
  Ruff `S` rules.
- **Cost implications**: None — local tooling.
- **Rollback procedure**: Revert the scaffold commit; delete `.venv`.
- **Definition of done**: `make check-tools`, `make fmt-check`, `make lint`,
  `make test` pass with real output.

### BOOT-3: ADR — Terraform state backend strategy

- **Objective**: Decide and record Terraform state storage (Azure Storage +
  container, key naming, locking).
- **Dependencies**: None.
- **Implementation steps**: Write `docs/adr/0002-terraform-state-backend.md`
  covering backend type (`azurerm`), key convention, network posture, and how
  the backend is bootstrapped without a chicken-and-egg remote-state
  dependency.
- **Expected files**: `docs/adr/0002-terraform-state-backend.md`.
- **Azure DevOps entity involved**: None.
- **Azure / Databricks identity and permissions**: Documents that data-plane
  access is Entra-based (Storage Blob Data Contributor), no Shared Key.
- **Terraform state key affected**: Defines the convention (`bootstrap.tfstate`
  today; forward `platform/dev.tfstate`, `databricks/dev.tfstate`,
  `applications/<name>/dev.tfstate`).
- **Validation commands**: `pre-commit run --files docs/adr/0002-terraform-state-backend.md`
- **Security implications**: Specifies network access and Entra-only
  authentication as the target.
- **Cost implications**: Single small Standard LRS account; <$1/month.
- **Rollback procedure**: Revert the ADR commit.
- **Definition of done**: ADR "Accepted"; unambiguous enough for BOOT-4.

### BOOT-4: Bootstrap Terraform module

- **Objective**: One-time Terraform (local state) that creates only the storage
  account and container used as the remote backend.
- **Dependencies**: BOOT-3.
- **Implementation steps**: Create `bootstrap/` with pinned `terraform`/
  `azurerm` versions; define resource group, storage account, container with
  `owner`/`environment=dev`/`expiry` tags; keep this module on local state by
  design; document the one-time, human-approved apply.
- **Expected files**: `bootstrap/versions.tf` (or `providers.tf`),
  `bootstrap/main.tf`, `bootstrap/variables.tf`, `bootstrap/outputs.tf`,
  `bootstrap/backend.tf`, `bootstrap/README.md`.
- **Azure DevOps entity involved**: None.
- **Azure / Databricks identity and permissions**: Applied by a human operator
  using their own Entra identity; no service principal yet.
- **Terraform state key affected**: `bootstrap.tfstate` (local until migrated).
- **Validation commands**:
  `terraform -chdir=bootstrap init -backend=false && terraform -chdir=bootstrap validate && tflint --chdir=bootstrap`
- **Security implications**: `allow_nested_items_to_be_public = false`, TLS 1.2
  min, HTTPS only, Entra data-plane auth. No apply as part of this task.
- **Cost implications**: Negligible, but the first billable resource — flag
  when apply is requested.
- **Rollback procedure**: Because state is local and the account carries a
  CanNotDelete lock, recovery follows `docs/runbooks/terraform-state-recovery.md`;
  never `terraform destroy`.
- **Definition of done**: `validate` and `tflint` pass; reviewed; not applied
  except by explicit human approval.

---

## Stage 1 — Core Azure landing zone

Goal: the smallest viable Azure landing zone — one resource group per
environment plus tagging and budget guardrails — all pure Terraform with no
CI/CD identity required yet. **(GitHub OIDC, formerly here, is removed and
deferred to Stage 11 per ADR 0003.)**

### AZ-1: Resource group + tagging module

- **Objective**: Reusable module for environment resource groups enforcing the
  mandatory tag schema (`owner`, `environment`, `expiry`).
- **Dependencies**: BOOT-4.
- **Implementation steps**: Create `infra/modules/resource-group/` with typed,
  validated variables (`environment` from an allowed set, `expiry` a validated
  date); instantiate for `dev` in `infra/environments/dev/main.tf`.
- **Expected files**: `infra/modules/resource-group/*.tf`,
  `infra/environments/dev/main.tf`.
- **Azure DevOps entity involved**: None (authored locally; applied via Stage 6
  CD pipeline once that exists, or by human approval before then).
- **Azure / Databricks identity and permissions**: No identity created; the
  future apply identity (Stage 4) will need Contributor at this RG scope.
- **Terraform state key affected**: `platform/dev.tfstate`.
- **Validation commands**:
  `terraform -chdir=infra/environments/dev validate && tflint --chdir=infra/environments/dev`
- **Security implications**: No public network access opened. Region UK South.
- **Cost implications**: Resource groups are free; establishes cost-attribution
  tagging for everything after.
- **Rollback procedure**: Revert the module commit; if applied, `terraform
  apply` of the prior revision (never `destroy` on shared RGs).
- **Definition of done**: Module validated; variable validation exercised; not
  applied.

### AZ-2: Cost budget + alerting

- **Objective**: An `azurerm_consumption_budget_resource_group` with alerts so
  experimentation cost is bounded and visible.
- **Dependencies**: AZ-1.
- **Implementation steps**: Define a small monthly budget and thresholds
  (50/80/100%); create an `azurerm_monitor_action_group` for notification.
- **Expected files**: `infra/modules/budget/*.tf`,
  `infra/environments/dev/budget.tf`.
- **Azure DevOps entity involved**: None.
- **Azure / Databricks identity and permissions**: None created; apply identity
  (Stage 4) needs Contributor at RG scope to create these.
- **Terraform state key affected**: `platform/dev.tfstate`.
- **Validation commands**: `terraform -chdir=infra/environments/dev validate`
- **Security implications**: Notification email passed via gitignored `.tfvars`
  or a non-secret pipeline variable — never committed literally.
- **Cost implications**: Exists to control cost risk; treat the budget as the
  hard ceiling for later experimentation stages.
- **Rollback procedure**: Revert commit; budgets carry no destructive risk.
- **Definition of done**: Module validated; budget documented in
  `docs/architecture.md`; not applied.

---

## Stage 2 — Platform security baseline

Goal: a governed secrets store, accessed by RBAC and workload identity, before
any workload needs a secret.

### AZ-3: Key Vault for workload secrets

- **Objective**: A Key Vault scoped to the dev resource group, RBAC-authorized,
  with no keys/secrets ever committed.
- **Dependencies**: AZ-1.
- **Implementation steps**: Create `infra/modules/key-vault/` with RBAC
  authorization, purge protection, and public network access default-deny
  (highlight this decision explicitly). Defer the *consumer* role grant
  ("Key Vault Secrets User") until the identity that needs it exists — the
  Databricks workload identity (Stage 8) or the Azure DevOps apply identity
  (Stage 4) — so identity creation and permission grant stay separately
  reviewable.
- **Expected files**: `infra/modules/key-vault/*.tf`,
  `infra/environments/dev/key-vault.tf`.
- **Azure DevOps entity involved**: None yet (grant wired in Stage 4/8).
- **Azure / Databricks identity and permissions**: Local operator gets "Key
  Vault Secrets Officer" for setup; automation identities get scoped
  "Secrets User" only, later.
- **Terraform state key affected**: `platform/dev.tfstate`.
- **Validation commands**:
  `terraform -chdir=infra/environments/dev validate && tflint --chdir=infra/environments/dev`
- **Security implications**: Public-network decision called out in the PR;
  prefer private endpoint / firewall restriction over fully public.
- **Cost implications**: Standard tier, per-operation; low. Note in the PR.
- **Rollback procedure**: Revert commit; purge protection means a deleted vault
  is recoverable within retention, never hard-deleted.
- **Definition of done**: Module validated; network decision documented; not
  applied.

---

## Stage 3 — Azure DevOps foundation

Goal: Azure Repos becomes the canonical repository with `main` protected by
branch policies, and Claude Code's local-Git workflow against Azure Repos is
documented. No pipelines yet.

**Status: migration to Azure Repos complete.** Azure Repos is the canonical
repository (`azure` remote / `origin`); GitHub (`github` remote) is retained
only as a temporary backup remote and owns no deployment. `main` requires a pull
request; direct pushes and policy bypasses are prohibited; the build-validation
policy slot is reserved for TFCI-2. Coordinates, authentication and branch
policy are recorded in `docs/runbooks/azure-repos-migration.md`. The remaining
ADO-1/ADO-3/ADO-4 configuration details below stay as the reference record.

### ADO-1: Provision the Azure DevOps organisation and project

- **Objective**: A ready Azure DevOps organisation and a project named
  `azure-ai-platform`.
- **Dependencies**: None (may run alongside Stage 0–2).
- **Exact implementation steps**:
  1. Create or confirm an Azure DevOps organisation backed by the same Entra
     tenant as the Azure subscription (Organization settings → Microsoft Entra
     → connect to the correct directory).
  2. Create a project named exactly `azure-ai-platform`, Git version control,
     private visibility.
  3. Set the project's default process and disable services not in use
     (Boards/Artifacts optional; Repos and Pipelines required).
  4. Record the organisation and project URLs in `docs/architecture.md`.
- **Expected files**: `docs/architecture.md` (updated).
- **Azure DevOps entity involved**: Organisation; Project `azure-ai-platform`.
- **Azure / Databricks identity and permissions**: Human creator uses their
  Entra identity (Project Collection Administrator). No service identity yet.
- **Terraform state key affected**: None.
- **Validation commands**: `az devops project show --project azure-ai-platform
  --org https://dev.azure.com/<org>` (read-only verification).
- **Security implications**: Bind the org to the corporate/primary Entra tenant
  so identity, conditional access and lifecycle are centrally governed. Do not
  enable third-party/OAuth app access unless needed.
- **Cost implications**: Azure DevOps free tier (5 users, 1 Microsoft-hosted
  parallel job) covers a personal platform; note if usage would exceed it.
- **Rollback procedure**: Delete the project (Project settings → Overview →
  Delete) — reversible within the recycle-bin retention window; no data yet.
- **Definition of done**: Organisation confirmed on the correct tenant; project
  `azure-ai-platform` exists and is verifiable via `az devops`.

### ADO-2: Create the Azure Repo and migrate local Git with full history
az identity list \
  --query "[?principalId=='$AZDO_PLAN_PRINCIPAL_ID'].{
    Name:name,
    ResourceGroup:resourceGroup,
    ClientId:clientId,
    PrincipalId:principalId,
    ResourceId:id
  }" \
  --output table
- **Objective**: Azure Repos holds the canonical repository with complete Git
  history; the local repo pushes to it.
- **Dependencies**: ADO-1.
- **Exact implementation steps**:
  1. Create an empty Git repository named `azure-ai-platform` in the project.
  2. Add it as a remote on the existing local clone:
     `git remote add azure https://<org>@dev.azure.com/<org>/azure-ai-platform/_git/azure-ai-platform`.
  3. Push **all** branches and tags to preserve history:
     `git push azure --all` and `git push azure --tags` (requires explicit
     human approval — `git push` is on the approval list).
  4. Confirm history depth matches local (`git log` count) and that merge
     commits `#1`/`#2` are present.
  5. Decide remote topology: make `azure` the primary remote; leave any GitHub
     remote in place but unused until Stage 11.
- **Expected files**: None in-repo (remote configuration); `docs/architecture.md`
  updated with the remote URL and topology.
- **Azure DevOps entity involved**: Repo `azure-ai-platform`.
- **Azure / Databricks identity and permissions**: Human pushes with their
  Entra identity over Git credential manager / Entra auth (no PAT).
- **Terraform state key affected**: None.
- **Validation commands**:
  `git ls-remote azure` and compare `git rev-list --count --all` before/after.
- **Security implications**: Authenticate Git via Entra (Git Credential Manager)
  rather than a PAT. Ensure no secrets exist in history before pushing
  (`trivy fs --scanners secret .` and a history scan).
- **Cost implications**: None (repo storage within free tier).
- **Rollback procedure**: Remote is additive; delete the Azure remote/repo to
  undo. Local history is untouched and remains the source of truth until
  cut-over is confirmed.
- **Definition of done**: `main` and all tags present in Azure Repos with
  identical history; remote documented.

### ADO-3: Configure default branch and branch policies

- **Objective**: `main` is the default branch, direct pushes are blocked, and
  merges require a reviewed pull request.
- **Dependencies**: ADO-2.
- **Exact implementation steps**:
  1. Set `main` as the repository default branch (Repos → Branches → set
     default).
  2. On `main`, add branch policies: **Require a minimum number of reviewers =
     1** (where supported by the license tier), with "reset votes on new
     changes" and "prohibit the requester from approving their own changes"
     where a second reviewer is unavailable.
  3. Require linked work items = optional; require comment resolution = on.
  4. Enforce "Require pull request" by removing the "Bypass policies when
     pushing" and "Contribute" push rights on `main` (see ADO-4) so direct
     pushes are blocked for everyone.
  5. Reserve a **Build validation** policy slot to be attached in Stage 5
     (TFCI-2) — leave a documented placeholder now; do not point it at a
     non-existent pipeline.
- **Expected files**: `docs/architecture.md` (policy summary); optional
  `docs/runbooks/azure-repos-branch-policy.md`.
- **Azure DevOps entity involved**: Repo branch policies on `main`.
- **Azure / Databricks identity and permissions**: Configured by Project
  Administrator; no cloud identity.
- **Terraform state key affected**: None.
- **Validation commands**:
  `az repos policy list --org https://dev.azure.com/<org> --project azure-ai-platform
  --repository-id <id>` (read-only verification of active policies).
- **Security implications**: Prevents unreviewed or unvalidated changes —
  including infrastructure — reaching `main`. Single-reviewer is a personal
  constraint; document it and require self-approval prohibition to compensate.
- **Cost implications**: None.
- **Rollback procedure**: Policies are configuration; disable or delete to
  revert. No data risk.
- **Definition of done**: `main` is default; PR required; direct push blocked;
  ≥1 reviewer enforced where the tier allows; build-validation placeholder
  documented for Stage 5.

### ADO-4: Repository permissions and Claude Code local-Git workflow

- **Objective**: Least-privilege repository permissions, plus documentation of
  how Claude Code operates against Azure Repos using ordinary local Git.
- **Dependencies**: ADO-3.
- **Exact implementation steps**:
  1. Define repository security: contributors get Contribute + Create Branch on
     feature branches; remove Force Push (rewrite history) and "Bypass policies"
     from all non-admin groups; restrict Delete/Manage permissions to
     administrators.
  2. Document the Claude Code workflow in `docs/architecture.md` /
     `docs/runbooks/`: Claude Code uses local Git only — creates a feature
     branch, commits, and (with explicit human approval) `git push azure` the
     branch; a human opens/approves the PR in Azure DevOps. Claude Code never
     pushes to `main`, never force-pushes, and never bypasses policies.
  3. Note that `az repos pr create` may be used to open (not complete) a PR,
     and that PR completion is a human, policy-gated action.
- **Expected files**: `docs/runbooks/azure-repos-workflow.md`,
  `docs/architecture.md` (updated).
- **Azure DevOps entity involved**: Repo security groups / permissions.
- **Azure / Databricks identity and permissions**: Human contributor identity
  via Entra; no automation identity.
- **Terraform state key affected**: None.
- **Validation commands**:
  `az repos permission list` (or Project settings → Repositories → Security
  review); confirm Force Push and Bypass are Deny/Not-set for contributors.
- **Security implications**: Enforces least privilege; guarantees history
  integrity; codifies that the agent has no path to bypass review.
- **Cost implications**: None.
- **Rollback procedure**: Permissions are configuration; restore prior ACLs.
- **Definition of done**: Permissions least-privilege; the Claude Code
  local-Git workflow is documented and matches enforced policy.

---

## Stage 4 — Azure DevOps workload identity federation

Goal: two Entra-federated Azure Resource Manager service connections — one for
`plan` (read-only + state access) and one for `apply` (scoped Contributor) —
with **no client secrets**.

### WIF-1: Identity model design and current-guidance ADR note

- **Objective**: Record the identity model and align to current Microsoft
  guidance before creating any connection.
- **Dependencies**: ADO-1.
- **Exact implementation steps**:
  1. Document, in an ADR addendum or `docs/architecture.md`, the distinctions:
     **application registration** (the Entra app object / global identity),
     **service principal** (the tenant-local instance of that app used for
     authZ), **managed identity** (Azure-managed identity with no app object,
     used by Azure resources such as the Databricks access connector), and
     **Azure DevOps service connection** (the pipeline-facing credential object
     that references an app/SP or managed identity and holds the federation
     subject).
  2. Specify **Entra-issued** workload identity federation for the service
     connections (the current recommended configuration), and explicitly
     **exclude deprecated Azure DevOps-issued federation** configurations.
  3. Define two identities: `sc-terraform-plan` and `sc-terraform-apply`, each a
     separate app registration + federated credential (subject scoped to the
     specific service connection / pipeline), never sharing an identity.
- **Expected files**: `docs/adr/0003-azure-devops-primary-cicd.md` (referenced),
  `docs/architecture.md` (identity model section).
- **Azure DevOps entity involved**: (Design for) two ARM service connections.
- **Azure / Databricks identity and permissions**: Defines two Entra app
  registrations / service principals; no permissions granted yet.
- **Terraform state key affected**: None (design task).
- **Validation commands**: `pre-commit run --files docs/architecture.md`;
  peer/human review against current Microsoft WIF documentation.
- **Security implications**: Establishes no-secrets, split plan/apply identities
  as the security control before anything is created.
- **Cost implications**: App registrations and WIF are free.
- **Rollback procedure**: Revert the doc commit.
- **Definition of done**: Model documented; Entra-issued WIF chosen;
  deprecated ADO-issued federation ruled out; two identities specified.

### WIF-2: Create the Terraform **plan** service connection (Entra WIF)

- **Objective**: A read-and-state-only service connection the CI pipeline uses
  for `terraform plan`.
- **Dependencies**: WIF-1, BOOT-4 (state container exists).
- **Exact implementation steps**:
  1. Create an Entra app registration `sp-tf-plan-dev` (requires approval —
     `az ad app create`) and a service principal for it.
  2. Add a **federated credential** with issuer = Azure DevOps' Entra-issued
     workload-identity issuer and subject scoped to the plan service
     connection / pipeline (no secret created).
  3. In Azure DevOps, create an ARM service connection
     `sc-terraform-plan` of type **Workload identity federation
     (Entra-issued)** referencing this app; scope it to the subscription.
  4. Grant roles (requires approval — `az role assignment create`):
     **Reader** at the required Azure scope (subscription or platform RG), and
     **Storage Blob Data Contributor** scoped to the Terraform **state
     container** only.
- **Expected files**: `infra/environments/dev/devops-identities.tf` (if the
  app/SP/federated-credential and role assignments are managed in Terraform),
  or a documented manual runbook if created out-of-band first.
- **Azure DevOps entity involved**: Service connection `sc-terraform-plan`.
- **Azure / Databricks identity and permissions**: `sp-tf-plan-dev` — Reader at
  the required scope; Storage Blob Data Contributor at the state-container
  scope. No write/Contributor rights on resources.
- **Terraform state key affected**: Reads all keys under the state container
  (e.g. `platform/dev.tfstate`); writes only the state **lease/lock**, not
  resources.
- **Validation commands**: A pipeline dry-run of `terraform init` + `terraform
  plan` using the connection; `az role assignment list --assignee <appId>`
  confirms only Reader + Storage Blob Data Contributor.
- **Security implications**: No client secret. Least privilege: plan identity
  cannot mutate infrastructure. State-container scope prevents access to other
  storage.
- **Cost implications**: Free (identity + role assignments).
- **Rollback procedure**: Delete the service connection and the two role
  assignments; delete the app registration. No infrastructure affected.
- **Definition of done**: Connection created via Entra-issued WIF, no secret;
  roles exactly Reader + Storage Blob Data Contributor (state container); a
  `plan` runs successfully.

### WIF-3: Create the Terraform **apply** service connection (Entra WIF)

- **Objective**: A separate, more privileged service connection used only by
  the CD pipeline for `terraform apply`.
- **Dependencies**: WIF-2.
- **Exact implementation steps**:
  1. Create a **separate** app registration `sp-tf-apply-dev` + service
     principal (approval required) with its own federated credential (subject
     scoped to the CD pipeline / `azure-ai-platform-dev` environment).
  2. Create ARM service connection `sc-terraform-apply` (Entra-issued WIF),
     restricted to the CD pipeline via connection security (pipeline
     authorization, not open to all pipelines).
  3. Grant roles (approval required): **Contributor scoped only to the target
     resource group(s)** for `dev`, plus **Storage Blob Data Contributor** on
     the state container. **Do not** grant Owner or User Access Administrator,
     and **do not** grant subscription-wide Contributor.
- **Expected files**: `infra/environments/dev/devops-identities.tf` (updated),
  or manual runbook.
- **Azure DevOps entity involved**: Service connection `sc-terraform-apply`
  (authorized only to the CD pipeline / dev environment).
- **Azure / Databricks identity and permissions**: `sp-tf-apply-dev` —
  Contributor at the **dev resource-group scope only**; Storage Blob Data
  Contributor at the state-container scope. Never Owner / User Access
  Administrator.
- **Terraform state key affected**: `platform/dev.tfstate` (read + write +
  lock).
- **Validation commands**: `az role assignment list --assignee <appId>`
  confirms RG-scoped Contributor only, no subscription scope, no Owner/UAA.
- **Security implications**: Separation of duties from the plan identity; blast
  radius limited to the dev RG; no secret. Pipeline-scoped connection
  authorization prevents other pipelines borrowing apply rights.
- **Cost implications**: Free.
- **Rollback procedure**: Delete the service connection, role assignments, and
  app registration; the plan path is unaffected.
- **Definition of done**: Distinct apply identity via Entra-issued WIF; RG-only
  Contributor + state access; no Owner/UAA; connection authorized to the CD
  pipeline only.

---

## Stage 5 — Terraform CI pipeline

Goal: `azure-pipelines/terraform-ci.yml` runs on pull requests, produces a
security-scanned `terraform plan` artifact using the **plan** connection, and
can never apply.

### TFCI-1: Author the Terraform CI pipeline

**Status: authored.** `azure-pipelines/terraform-ci.yml` (plus
`azure-pipelines/templates/terraform-checks.yml`, `scripts/ci/install-tools.sh`
and `scripts/ci/terraform-plan.sh`) is in the repository. It is plan-only, uses
the plan service connection **`sc-azure-terraform-plan`** (the concrete name for
the generic `sc-terraform-plan` below — a user-assigned managed identity with
Entra workload identity federation), obtains a fresh OIDC token per run, and
publishes only a redacted `plan.txt` and validation logs. See
`docs/runbooks/terraform-ci.md`. Creating the pipeline object and attaching the
build-validation policy (TFCI-2) remain manual Azure DevOps actions.

- **Objective**: A PR-triggered pipeline reproducing local `make` checks plus a
  plan artifact.
- **Dependencies**: WIF-2, ADO-3.
- **Exact implementation steps**:
  1. Create `azure-pipelines/terraform-ci.yml` triggered on **pull requests**
     targeting `main` (PR trigger via branch policy build validation; no CI
     trigger on `main` for this file).
  2. Steps, reusing Makefile targets where practical:
     `make fmt-check` (`terraform fmt -check -recursive`); `terraform init`
     against the **remote azurerm backend** (Entra auth via the plan service
     connection); `make validate`; `tflint` (`make lint`);
     Trivy IaC scan (`make security`, `--scanners misconfig,secret,vuln`,
     fail on HIGH/CRITICAL); `terraform plan -out=tfplan` via
     `sc-terraform-plan`; render `terraform show -no-color tfplan > plan.txt`.
  3. `PublishPipelineArtifact` the human-readable `plan.txt` (and optionally the
     binary `tfplan`) as artifact `terraform-plan`.
  4. **No `terraform apply`** step exists anywhere in this file.
  5. Use pipeline `concurrency` (a lock group keyed to the state key) and rely
     on azurerm state locking so two runs cannot plan/lock simultaneously.
- **Expected files**: `azure-pipelines/terraform-ci.yml`.
- **Azure DevOps entity involved**: Pipeline `terraform-ci`; service connection
  `sc-terraform-plan`.
- **Azure / Databricks identity and permissions**: `sp-tf-plan-dev` — Reader +
  Storage Blob Data Contributor (state container). Read-only on infrastructure.
- **Terraform state key affected**: Reads `platform/dev.tfstate`; acquires the
  state lock during plan only.
- **Validation commands**: Run the pipeline on a test PR; confirm it fails on a
  deliberately mis-formatted file and on an injected HIGH misconfiguration;
  confirm the `terraform-plan` artifact is published.
- **Security implications**: No secrets; plan identity cannot mutate; security
  scan gates the PR. `id-token`/federation handled by the service connection,
  not stored credentials.
- **Cost implications**: Microsoft-hosted parallel-job minutes (free tier);
  negligible at personal PR volume.
- **Rollback procedure**: Delete/disable the pipeline; the YAML is revertible.
  No infrastructure changed.
- **Definition of done**: Pipeline runs on PRs, performs fmt/init/validate/
  tflint/Trivy/plan, publishes the plan artifact, contains no apply, and uses
  concurrency + state locking.

### TFCI-2: Attach CI as a build-validation branch policy

- **Objective**: Make `terraform-ci` a required check for merging to `main`,
  filling the placeholder from ADO-3.
- **Dependencies**: TFCI-1.
- **Exact implementation steps**:
  1. Add a **Build validation** policy on `main` referencing the `terraform-ci`
     pipeline, set to **Required**, automatic queue on PR, expire on source
     update.
  2. Confirm a PR cannot complete while the check is failing or stale.
- **Expected files**: `docs/architecture.md` (policy note).
- **Azure DevOps entity involved**: Branch policy (build validation) on `main`.
- **Azure / Databricks identity and permissions**: None beyond the pipeline's
  own connection.
- **Terraform state key affected**: None directly.
- **Validation commands**: `az repos policy list ...` shows an active,
  required build-validation policy bound to `terraform-ci`.
- **Security implications**: Guarantees no infrastructure change merges without
  a passing plan + security scan.
- **Cost implications**: None beyond CI minutes.
- **Rollback procedure**: Set the policy to optional or delete it.
- **Definition of done**: Build validation required on `main`; a failing CI
  run blocks merge on a test PR.

---

## Stage 6 — Terraform CD pipeline

Goal: `azure-pipelines/terraform-cd.yml` runs only after merge to `main`,
deploys to **dev only**, and applies solely after manual approval on the
`azure-ai-platform-dev` Environment using the **apply** connection.

### TFCD-1: Create the `azure-ai-platform-dev` Environment with approval

- **Objective**: An Azure DevOps Environment that gates apply behind human
  approval.
- **Dependencies**: WIF-3.
- **Exact implementation steps**:
  1. Create an Environment named exactly `azure-ai-platform-dev`.
  2. Add an **Approvals and checks → Approvals** gate requiring a named human
     approver (not the requester) before any deployment job targeting the
     environment runs.
  3. Optionally add a **Business hours** or **Exclusive lock** check to prevent
     concurrent applies.
- **Expected files**: `docs/architecture.md` (environment + approval note).
- **Azure DevOps entity involved**: Environment `azure-ai-platform-dev`
  (+ approval check).
- **Azure / Databricks identity and permissions**: Approval performed by a
  human Entra identity; deployment job uses `sc-terraform-apply`.
- **Terraform state key affected**: None (gate only).
- **Validation commands**: `az devops` / portal shows the environment with a
  required approval; a deployment job pauses pending approval.
- **Security implications**: The approval **is** the human-approval control for
  `terraform apply` mandated by `CLAUDE.md`. No auto-approval / bypass.
- **Cost implications**: None.
- **Rollback procedure**: Delete the environment or approval check
  (configuration only).
- **Definition of done**: Environment exists; a deployment cannot proceed
  without a human approving.

### TFCD-2: Author the Terraform CD pipeline

**Status: authored.** `azure-pipelines/terraform-cd.yml` (plus
`scripts/ci/check-destructive-plan.sh` and `scripts/ci/terraform-cd-apply.sh`,
reusing `scripts/ci/install-tools.sh` and `scripts/ci/terraform-plan.sh`) is in
the repository. It triggers **only on merge to `main`**, plans the exact merged
commit with the plan-only connection **`sc-azure-terraform-plan`**, publishes a
redacted `plan.txt`, and fails on unexpected destructive changes via a
lightweight `terraform show -json` guard (a lab guardrail, not a policy engine).
Behind the **`azure-ai-platform-dev`** Environment approval it applies the
**exact saved binary plan** (promoted as a short-lived pipeline artifact; never
re-planned) with the separate apply connection **`sc-azure-terraform-apply`**
(Contributor on `rg-aiplatform-bootstrap-dev` only + Storage Blob Data
Contributor on the tfstate container), then runs a post-apply
`terraform plan -detailed-exitcode` drift check. No storage key, SAS token,
client secret or PAT is used; no `terraform destroy` is run. The expected apply
is currently a **no-op** proving the control path. Creating/authorising the
pipeline and confirming the Environment approval remain manual Azure DevOps
actions. See `docs/runbooks/terraform-cd.md`. Post-apply smoke tests beyond the
drift check (e.g. `az group show`) are deferred until there are dev resources
worth asserting on.

- **Objective**: Merge-to-main pipeline that plans, waits for approval, then
  applies to dev only.
- **Dependencies**: TFCD-1, TFCI-1.
- **Exact implementation steps**:
  1. Create `azure-pipelines/terraform-cd.yml` triggered **only** on merge to
     `main` (branch `main`, no PR trigger).
  2. Stage A (build): `terraform init` (remote backend) + `terraform plan
     -out=tfplan` via `sc-terraform-apply`; publish `tfplan` + `plan.txt`.
  3. Stage B (deploy) as a **deployment job** targeting environment
     `azure-ai-platform-dev` (triggers the approval gate). After approval,
     either `terraform apply tfplan` (the reviewed plan) **or** re-run
     `terraform plan` and display it immediately before `apply` so the approver
     sees current intent; never apply an unseen plan.
  4. Restrict to **dev**: no production variable group, environment, or service
     connection is referenced. Add an explicit guard that fails if the target
     is anything other than dev.
  5. After apply, run **smoke tests** (Stage C): e.g. `az group show` on the
     dev RG, `az storage account show`, and a `terraform plan` that must report
     **no changes** (drift check).
  6. Reuse Makefile targets where practical; use concurrency + azurerm state
     locking to serialize applies.
- **Expected files**: `azure-pipelines/terraform-cd.yml`;
  `scripts/smoke/terraform-dev-smoke.sh` (optional).
- **Azure DevOps entity involved**: Pipeline `terraform-cd`; environment
  `azure-ai-platform-dev`; service connection `sc-terraform-apply`.
- **Azure / Databricks identity and permissions**: `sp-tf-apply-dev` —
  RG-scoped Contributor + state-container Storage Blob Data Contributor.
- **Terraform state key affected**: `platform/dev.tfstate` (read/write/lock).
- **Validation commands**: On a controlled test merge, confirm the pipeline
  pauses for approval, applies only after approval, and the post-apply
  `terraform plan` reports no drift.
- **Security implications**: Apply is human-gated; production is unreachable;
  the plan shown to the approver matches what applies; no secrets.
- **Cost implications**: Applies real (billable) resources — the approver must
  review the plan for unexpectedly expensive resources before approving.
- **Rollback procedure**: See TFCD-3. In summary: re-apply the previous known-
  good Terraform revision through the same pipeline; never `terraform destroy`
  on shared state. State recovery via `docs/runbooks/terraform-state-recovery.md`.
- **Definition of done**: Pipeline triggers only on merge to `main`, applies to
  dev only after approval using a reviewed/re-displayed plan, runs smoke tests,
  and cannot reach production.

### TFCD-3: Document rollback and state recovery

- **Objective**: A written, tested rollback and state-recovery procedure for
  the CD path.
- **Dependencies**: TFCD-2.
- **Exact implementation steps**:
  1. Extend `docs/runbooks/terraform-state-recovery.md` with a CD-specific
     section: how to revert a bad apply (revert the merge → the pipeline plans
     the reversal → approve), how to restore a prior state blob version (blob
     versioning + soft delete), and how to recover the state lock if a run is
     interrupted (`terraform force-unlock` guidance with cautions).
  2. Record the CanNotDelete lock and `prevent_destroy` interactions.
- **Expected files**: `docs/runbooks/terraform-state-recovery.md` (updated).
- **Azure DevOps entity involved**: Pipeline `terraform-cd` (referenced).
- **Azure / Databricks identity and permissions**: Recovery uses the human
  operator's Entra identity with state-container data access.
- **Terraform state key affected**: `platform/dev.tfstate` (recovery target).
- **Validation commands**: Dry-run a state-version restore in a scratch
  container; verify `force-unlock` steps against a deliberately abandoned lock.
- **Security implications**: Recovery must not introduce Shared Key or PAT use;
  it relies on Entra data access only.
- **Cost implications**: None beyond negligible storage.
- **Rollback procedure**: N/A (this task documents rollback).
- **Definition of done**: Runbook covers apply-revert, state-version restore,
  and lock recovery; steps validated in a scratch environment.

---

## Stage 7 — Backend authentication hardening

Goal: the Terraform backend and both pipeline identities authenticate to state
storage via Microsoft Entra only; Shared Key is disabled after every path is
proven; state protection features are retained.

### HARD-1: Grant Entra data access to pipeline and local identities

- **Objective**: All identities that touch state have **Storage Blob Data
  Contributor** at the state-container scope.
- **Dependencies**: WIF-2, WIF-3.
- **Exact implementation steps**:
  1. Assign **Storage Blob Data Contributor** at the **state-container** scope
     to `sp-tf-plan-dev` and `sp-tf-apply-dev` (approval required) — confirm/
     reconcile with WIF-2/WIF-3 so scope is container, not account/subscription.
  2. Assign the local administrator (the human operator) the equivalent Entra
     Blob data role at the same scope.
- **Expected files**: `infra/environments/dev/state-access.tf` (if managed in
  Terraform) or a documented runbook.
- **Azure DevOps entity involved**: Both service connections (consumers).
- **Azure / Databricks identity and permissions**: plan SP, apply SP, and local
  admin — Storage Blob Data Contributor at the state-container scope only.
- **Terraform state key affected**: Data-plane access to all keys under the
  container (`bootstrap.tfstate`, `platform/dev.tfstate`, …).
- **Validation commands**: `az role assignment list --scope <container-scope>`
  shows exactly the three assignments at container scope.
- **Security implications**: Container-scoped, not account-scoped; no Shared
  Key; least privilege for data-plane state access.
- **Cost implications**: Free.
- **Rollback procedure**: Remove the role assignments (reverts to prior access;
  ensure at least one working path remains before removing any).
- **Definition of done**: Plan SP, apply SP, and local admin all hold
  container-scoped Blob data access.

### HARD-2: Move the azurerm backend to Entra authentication and verify

- **Objective**: The backend uses `use_azuread_auth = true` and works for local
  and pipeline runs before Shared Key is touched.
- **Dependencies**: HARD-1.
- **Exact implementation steps**:
  1. Ensure the azurerm backend config (`backend/dev.hcl` and/or `backend.tf`)
     sets `use_azuread_auth = true` and does not rely on `access_key`.
  2. Locally: `terraform init -reconfigure` then `terraform plan` for the
     bootstrap and platform configs; confirm Entra auth succeeds.
  3. In pipelines: run `terraform-ci` (plan SP) and a controlled `terraform-cd`
     plan (apply SP) and confirm both authenticate via Entra.
- **Expected files**: `backend/dev.hcl` (updated), `bootstrap/backend.tf` /
  `infra/environments/dev/backend.tf` (updated) — configuration only, no
  infrastructure resource changes.
- **Azure DevOps entity involved**: Pipelines `terraform-ci`, `terraform-cd`.
- **Azure / Databricks identity and permissions**: plan/apply SPs + local admin
  via the HARD-1 grants.
- **Terraform state key affected**: `bootstrap.tfstate`, `platform/dev.tfstate`
  (re-init with Entra auth; no state migration/rename).
- **Validation commands**: `terraform init -reconfigure` + `terraform plan`
  locally and in both pipelines all succeed with `use_azuread_auth = true`.
- **Security implications**: Removes reliance on account keys for state access.
  Per `CLAUDE.md`, this touches backend **configuration**, not the Terraform
  infrastructure resources.
- **Cost implications**: None.
- **Rollback procedure**: Revert the backend config to the prior working
  revision and `terraform init -reconfigure`.
- **Definition of done**: Local + both pipelines init/plan successfully under
  Entra auth; no Shared Key used by any path.

### HARD-3: Disable Shared Key and validate recovery

- **Objective**: Turn off Shared Key only after every path works, while
  retaining versioning, soft delete, `prevent_destroy`, and the deletion lock.
- **Dependencies**: HARD-2.
- **Exact implementation steps**:
  1. Confirm recovery access **before** disabling: perform a state-version
     read/restore test using Entra auth (per TFCD-3 runbook).
  2. Set `shared_access_key_enabled = false` on the state storage account as an
     isolated, reviewed `terraform apply` (human-approved).
  3. Re-run `terraform init -reconfigure` + `plan` locally and in both pipelines
     to confirm Entra auth still works.
  4. Validate recovery access **after** disabling (repeat the version-restore
     test). Confirm blob versioning, blob/container soft delete,
     `prevent_destroy`, and the Azure CanNotDelete lock remain in effect.
- **Expected files**: `bootstrap/main.tf` (the `shared_access_key_enabled`
  flag), reviewed separately; runbook updates.
- **Azure DevOps entity involved**: `terraform-cd` (applies the flag under
  approval) — or a human-run apply.
- **Azure / Databricks identity and permissions**: apply SP (or local admin)
  with Contributor to change the account property; Entra data access to verify.
- **Terraform state key affected**: `bootstrap.tfstate` (the account holding
  all state); no key rename.
- **Validation commands**: Post-change `terraform plan` (no drift), a successful
  Entra-auth state read, and `az storage account show` confirming
  `allowSharedKeyAccess = false` with soft-delete/versioning still enabled.
- **Security implications**: Eliminates the last long-lived shared-key path.
  Recovery validated on both sides of the change so state can never be locked
  out. Deletion lock + `prevent_destroy` retained.
- **Cost implications**: None.
- **Rollback procedure**: If any path fails, re-enable Shared Key via a reviewed
  apply immediately; investigate before re-attempting. State protection
  features must never be removed to recover.
- **Definition of done**: Shared Key disabled; local + pipeline auth verified;
  recovery validated before and after; versioning/soft delete/`prevent_destroy`/
  lock all retained.

---

## Stage 8 — Azure Databricks platform provisioning

Goal: a governed Databricks workspace provisioned by Terraform, with managed
identities / access connectors, an initial Unity Catalog structure, and CI/CD
service principals — no PATs anywhere.

### DBX-1: Databricks workspace via Terraform

- **Objective**: Provision the workspace (durable infra) with Terraform,
  premium tier, no persistent all-purpose clusters.
- **Dependencies**: AZ-1, WIF-3 (apply identity), HARD-2 (Entra state auth).
- **Exact implementation steps**:
  1. Create `infra/modules/databricks-workspace/`
     (`azurerm_databricks_workspace`, premium SKU, managed RG naming, tags).
  2. Instantiate for `dev` under `databricks/dev.tfstate`.
- **Expected files**: `infra/modules/databricks-workspace/*.tf`,
  `infra/environments/dev/databricks.tf`.
- **Azure DevOps entity involved**: Applied via `terraform-cd` (apply SC).
- **Azure / Databricks identity and permissions**: apply SP needs Contributor
  at the dev RG; workspace itself will use a managed identity (DBX-2).
- **Terraform state key affected**: `databricks/dev.tfstate`.
- **Validation commands**:
  `terraform -chdir=infra/environments/dev validate && tflint --chdir=infra/environments/dev`
- **Security implications**: No PATs generated; Databricks-side auth uses Entra
  + workload identity end-to-end. Workspace network posture highlighted in PR.
- **Cost implications**: Premium workspace is free to create; cost comes from
  compute — clusters must be job-scoped / scale-to-zero, never persistent.
- **Rollback procedure**: Revert the module; never `terraform destroy` a
  workspace with governed data — remove via reviewed apply if truly needed.
- **Definition of done**: Module validated; state key `databricks/dev.tfstate`;
  not applied without approval.

### DBX-2: Managed identity / access connector + Unity Catalog structure

- **Objective**: Configure Databricks-to-Azure access via managed identity /
  access connector and define the initial Unity Catalog structure.
- **Dependencies**: DBX-1, AZ-3.
- **Exact implementation steps**:
  1. Create an **Access Connector for Azure Databricks** (managed identity) and
     grant it Storage Blob Data Contributor on the UC storage container.
  2. Define the Unity Catalog metastore, metastore assignment, a `dev` catalog,
     and schemas for agent data/artifacts; grant least-privilege catalog
     permissions to the CI/CD service principal (DBX-3), not `ALL PRIVILEGES`.
- **Expected files**: `infra/modules/unity-catalog/*.tf`,
  `infra/environments/dev/unity-catalog.tf`.
- **Azure DevOps entity involved**: Applied via `terraform-cd`.
- **Azure / Databricks identity and permissions**: Access connector **managed
  identity** for storage; no storage account keys. CI/CD SP gets scoped catalog
  grants.
- **Terraform state key affected**: `databricks/dev.tfstate`.
- **Validation commands**: `terraform -chdir=infra/environments/dev validate`.
- **Security implications**: UC storage uses managed identity, not keys;
  distinguishes durable Terraform-managed governance from workloads (DBX vs
  bundles).
- **Cost implications**: Metastore has no direct cost; storage minimal.
- **Rollback procedure**: Revert; UC objects removed via reviewed apply only.
- **Definition of done**: Access connector + UC structure validated; managed
  identity used; catalog/schema naming documented in `docs/architecture.md`.

### DBX-3: CI/CD service principals + Azure DevOps ↔ Databricks WIF

- **Objective**: Service principals for pipeline-driven Databricks deployment,
  federated where supported — no Databricks PATs.
- **Dependencies**: DBX-2, WIF-1.
- **Exact implementation steps**:
  1. Create a Databricks service principal for CI/CD (added to the workspace and
     the relevant UC grants).
  2. Configure **Azure DevOps → Azure Databricks workload identity
     federation** where supported (OIDC/Entra-federated service connection or
     the Databricks OAuth federation path), so pipelines obtain short-lived
     tokens; **avoid Databricks PATs entirely**.
  3. Document the fallback if federation is unavailable in a region/feature:
     an Entra-federated SP with OAuth (still no static PAT).
- **Expected files**: `infra/environments/dev/databricks-cicd-identity.tf`,
  `docs/architecture.md` (identity chain).
- **Azure DevOps entity involved**: A Databricks-targeted service connection
  (federated).
- **Azure / Databricks identity and permissions**: Databricks SP with scoped
  workspace + UC privileges; no PAT.
- **Terraform state key affected**: `databricks/dev.tfstate`.
- **Validation commands**: A pipeline `databricks auth` / `databricks bundle
  validate` step authenticates via federation (no PAT env var present).
- **Security implications**: Removes long-lived Databricks tokens; distinguishes
  durable Terraform-managed resources from Bundle-managed workloads.
- **Cost implications**: Free (identities).
- **Rollback procedure**: Delete the SP + federation config; workspace infra
  unaffected.
- **Definition of done**: CI/CD SP exists; ADO↔Databricks federation configured
  (or documented fallback); no PAT anywhere.

---

## Stage 9 — Databricks workload CI/CD

Goal: a sample Databricks Declarative Automation Bundle deployed through Azure
Pipelines using a service principal, with dev/prod-shaped targets, tests, and
prod approval.

### DAB-1: Sample Declarative Automation Bundle with dev/prod targets

- **Objective**: A bundle skeleton for application workloads, separate from
  Terraform-managed infra.
- **Dependencies**: DBX-3.
- **Exact implementation steps**:
  1. Author `bundles/agent-jobs/databricks.yml` with `development` and
     `production`-shaped targets (prod-shaped = same structure, still pointing
     at non-production compute initially).
  2. Add a placeholder job in `bundles/agent-jobs/resources/`; keep source
     outside DBFS root; job clusters job-scoped/ephemeral.
- **Expected files**: `bundles/agent-jobs/databricks.yml`,
  `bundles/agent-jobs/resources/*.yml`.
- **Azure DevOps entity involved**: None yet (validated by pipeline in DAB-2).
- **Azure / Databricks identity and permissions**: Validated with the CI/CD SP
  (federated), no PAT.
- **Terraform state key affected**: None (bundles are not Terraform state).
- **Validation commands**: `databricks bundle validate --target development`
  (auth-only, read-only; no deploy).
- **Security implications**: `databricks bundle deploy` requires approval; this
  task stops at validate. Bundle files outside DBFS root.
- **Cost implications**: No cost until a job runs; clusters job-scoped.
- **Rollback procedure**: Revert the bundle files.
- **Definition of done**: `databricks bundle validate` passes for both targets;
  not deployed.

### DAB-2: Bundle CI/CD through Azure Pipelines

- **Objective**: Validate bundles on PRs and deploy them through Azure
  Pipelines with a service principal, gating prod behind approval.
- **Dependencies**: DAB-1, TFCD-1 (environment/approval pattern).
- **Exact implementation steps**:
  1. Add `azure-pipelines/databricks-ci.yml`: on PR, run `databricks bundle
     validate` (federated SP) plus unit tests.
  2. Add `azure-pipelines/databricks-cd.yml`: on merge to `main`, deploy the
     bundle to the **development** target via the CI/CD SP; run integration +
     smoke tests after deploy.
  3. Gate any **production-shaped** deployment behind an Azure DevOps
     Environment approval (reuse the `azure-ai-platform-dev` pattern; add
     `azure-ai-platform-prod` only when prod is actually introduced).
  4. Forbid manual `databricks bundle deploy` from developer workstations
     except to a personal **development** target; document this.
- **Expected files**: `azure-pipelines/databricks-ci.yml`,
  `azure-pipelines/databricks-cd.yml`, `tests/integration/`, `tests/` smoke.
- **Azure DevOps entity involved**: Pipelines `databricks-ci`/`databricks-cd`;
  Databricks federated service connection; Environment approval for prod-shaped.
- **Azure / Databricks identity and permissions**: CI/CD Databricks SP
  (federated), scoped workspace/UC privileges; no PAT.
- **Terraform state key affected**: None.
- **Validation commands**: PR pipeline runs `bundle validate` + unit tests;
  merge pipeline deploys to development and runs integration/smoke tests;
  prod-shaped deploy pauses for approval.
- **Security implications**: Only pipelines (not workstations) deploy to shared
  targets; prod-shaped requires approval; federated auth only.
- **Cost implications**: Job-scoped compute during tests; keep runs small.
- **Rollback procedure**: Redeploy the previous bundle revision via the
  pipeline; `databricks bundle destroy` requires explicit approval and is not
  used for routine rollback.
- **Definition of done**: PR validation + merge deploy to development work with
  a federated SP; unit/integration/smoke tests run; prod-shaped deploy is
  approval-gated; workstation deploys restricted to personal dev targets.

---

## Stage 10 — First AI and agentic POC

Goal: one narrow RAG or tool-using agent, deployed through the Azure DevOps +
Databricks pipeline, with evaluation, observability, and hard guardrails.

### POC-1: Python 3.12 project with uv/Ruff/mypy/pytest

- **Objective**: The agent's Python package and toolchain (may reuse BOOT-2
  scaffold).
- **Dependencies**: BOOT-2.
- **Exact implementation steps**: Create `src/agents/first_agent/` (Python
  3.12, `uv`), configure Ruff/mypy/pytest, add typed interfaces.
- **Expected files**: `src/agents/first_agent/*.py`, `pyproject.toml` (updated).
- **Azure DevOps entity involved**: `databricks-ci` (runs tests) later.
- **Azure / Databricks identity and permissions**: None (local dev).
- **Terraform state key affected**: None.
- **Validation commands**:
  `make fmt-check && make lint && uv run mypy src/agents/first_agent && uv run pytest`.
- **Security implications**: Ruff `S` rules on; no secrets in code.
- **Cost implications**: Local only.
- **Rollback procedure**: Revert the package commit.
- **Definition of done**: Type-checked, linted, tested scaffold.

### POC-2: Narrow RAG / tool-using agent with guardrails, eval and telemetry

- **Objective**: One narrow agent with an explicit tool allowlist, evaluation
  datasets, and full observability; no infrastructure authority.
- **Dependencies**: POC-1.
- **Exact implementation steps**:
  1. Implement one narrow RAG or tool-using agent; treat model output as
     untrusted; validate tool arguments against schemas; enforce an explicit
     allowlist, per-tool timeouts and retry limits.
  2. Add evaluation datasets under `tests/evaluation/`; capture token usage,
     latency, errors, and tool calls via structured logging (scrub secrets).
  3. Guardrail: the allowlist must exclude every `CLAUDE.md` approval-listed
     command (`terraform apply/destroy`, `az role assignment …`,
     `databricks bundle deploy/destroy`, `git push`, `gh pr merge`) so the
     agent **cannot** execute infrastructure changes.
  4. Add cost and security checks (token-budget cap, Ruff `S`, dependency scan).
- **Expected files**: `src/agents/first_agent/{agent,tools,telemetry}.py`,
  `tests/evaluation/*`.
- **Azure DevOps entity involved**: `databricks-ci` runs eval/tests on PR.
- **Azure / Databricks identity and permissions**: Runs under the job's
  workload identity; no infra rights.
- **Terraform state key affected**: None.
- **Validation commands**: `uv run pytest tests/evaluation` (green); manual
  review of the allowlist against `CLAUDE.md`.
- **Security implications**: Agent has no path to consequential infra actions;
  logs exclude secrets; token budget caps runaway cost.
- **Cost implications**: Eval incurs token cost — keep the set small; note
  per-run cost in the PR.
- **Rollback procedure**: Revert the agent commit; no external state.
- **Definition of done**: Eval suite green; telemetry captured; allowlist
  reviewed; cost/security checks in place.

### POC-3: Deploy the agent through the Azure DevOps + Databricks pipeline

- **Objective**: Ship the agent as a Databricks job via the Stage 9 bundle
  pipeline.
- **Dependencies**: POC-2, DAB-2.
- **Exact implementation steps**: Add a job resource to `bundles/agent-jobs/
  resources/` referencing `src/agents/first_agent`; deploy to the development
  target via `databricks-cd`; require approval for any prod-shaped run; cap job
  cluster workers/node types.
- **Expected files**: `bundles/agent-jobs/resources/first-agent-job.yml`.
- **Azure DevOps entity involved**: `databricks-cd`; Databricks federated SC;
  prod-shaped approval environment.
- **Azure / Databricks identity and permissions**: CI/CD Databricks SP
  (federated), scoped; no PAT.
- **Terraform state key affected**: None.
- **Validation commands**: `databricks bundle validate --target development`;
  pipeline deploy to development + smoke test.
- **Security implications**: Job cluster policy caps blast radius; deploy is
  pipeline-only.
- **Cost implications**: Job-scoped compute per run; document expected cost.
- **Rollback procedure**: Redeploy the previous bundle revision via pipeline.
- **Definition of done**: Agent runs as a development job via the pipeline with
  telemetry; prod-shaped run approval-gated; deploy not manual.

---

## Stage 11 — Optional GitHub comparison

**Begin only after the Azure DevOps implementation (Stages 3–10) is stable.**
GitHub is a comparison lab only and must never own deployment to the Azure
DevOps-owned `dev` environment (ADR 0003: no environment has two deployment
owners).

### GH-1: Mirror to GitHub and add plan-only GitHub Actions (OIDC)

- **Objective**: Optionally mirror the repo to GitHub and run **plan-only**
  validation via GitHub Actions using GitHub OIDC, isolated from the Azure
  DevOps-owned environment.
- **Dependencies**: Stages 3–10 stable.
- **Exact implementation steps**:
  1. Optionally add a GitHub remote and mirror/push the repository.
  2. Create an Entra app + federated credential trusting GitHub's OIDC issuer
     (`repo:<org>/<repo>:ref:refs/heads/main` and a PR pattern); no secrets.
  3. Add `.github/workflows/terraform-plan.yml` doing **plan only** (fmt, init,
     validate, tflint, Trivy, `terraform plan`) — **no apply**.
  4. Isolate state: use a separate `github-lab/dev.tfstate` key and a distinct
     `github-lab` scope/environment so GitHub cannot plan or apply against the
     Azure DevOps-owned `platform/dev.tfstate`.
  5. Explicitly deny GitHub any Contributor/apply role on the ADO-owned dev RG.
- **Expected files**: `.github/workflows/terraform-plan.yml`;
  `infra/environments/github-lab/*` (separate lab scope).
- **Azure DevOps entity involved**: None (GitHub side); ADO ownership preserved.
- **Azure / Databricks identity and permissions**: GitHub OIDC SP — Reader +
  Storage Blob Data Contributor on the **github-lab** state container only;
  never Contributor on the ADO-owned dev RG.
- **Terraform state key affected**: `github-lab/dev.tfstate` only (never
  `platform/dev.tfstate`).
- **Validation commands**: `actionlint .github/workflows/terraform-plan.yml`;
  confirm the workflow can plan the lab scope and **cannot** apply anywhere.
- **Security implications**: Enforces single deployment owner per environment;
  GitHub is read/plan-only against an isolated lab state; no secrets.
- **Cost implications**: GitHub-hosted runner minutes (free tier) + a tiny
  separate lab state; negligible.
- **Rollback procedure**: Delete the workflow, the GitHub OIDC SP, and the lab
  scope; the ADO path is untouched throughout.
- **Definition of done**: GitHub Actions plan-only runs via OIDC against an
  isolated `github-lab` state; GitHub has no apply rights on any ADO-owned
  environment.

### GH-2: Comparison and platform-decision ADR

- **Objective**: Compare the two platforms and record a decision.
- **Dependencies**: GH-1.
- **Exact implementation steps**:
  1. Compare developer experience, policy/branch protection, identity/WIF,
     approvals/environments, observability, and maintenance overhead.
  2. Write `docs/adr/0004-platform-comparison-decision.md` ending with a clear
     decision (confirm Azure DevOps primary, retain/retire the GitHub lab).
- **Expected files**: `docs/adr/0004-platform-comparison-decision.md`,
  `docs/architecture.md` (comparison summary).
- **Azure DevOps entity involved**: None (documentation).
- **Azure / Databricks identity and permissions**: None.
- **Terraform state key affected**: None.
- **Validation commands**: `pre-commit run --files docs/adr/0004-platform-comparison-decision.md`.
- **Security implications**: None (documentation); decision must reaffirm the
  single-deployment-owner and no-long-lived-credential rules.
- **Cost implications**: None.
- **Rollback procedure**: Revert the ADR commit.
- **Definition of done**: Comparison documented; ADR 0004 records a clear,
  justified platform decision.

---

## Summary table

| ID | Stage | Objective | Depends on |
|----|-------|-----------|------------|
| BOOT-1 | 0 Bootstrap | README + architecture docs | — |
| BOOT-2 | 0 Bootstrap | Python project scaffold | — |
| BOOT-3 | 0 Bootstrap | ADR: state backend strategy | — |
| BOOT-4 | 0 Bootstrap | Bootstrap Terraform (state storage) | BOOT-3 |
| AZ-1 | 1 Core Azure | Resource group + tagging module | BOOT-4 |
| AZ-2 | 1 Core Azure | Cost budget + alerting | AZ-1 |
| AZ-3 | 2 Security baseline | Key Vault | AZ-1 |
| ADO-1 | 3 Azure DevOps foundation | Org + project `azure-ai-platform` | — |
| ADO-2 | 3 Azure DevOps foundation | Azure Repo + migrate local Git (history) | ADO-1 |
| ADO-3 | 3 Azure DevOps foundation | Default branch + branch policies | ADO-2 |
| ADO-4 | 3 Azure DevOps foundation | Repo permissions + Claude Code workflow | ADO-3 |
| WIF-1 | 4 ADO WIF | Identity model + current-guidance ADR note | ADO-1 |
| WIF-2 | 4 ADO WIF | Plan service connection (Entra WIF) | WIF-1, BOOT-4 |
| WIF-3 | 4 ADO WIF | Apply service connection (Entra WIF) | WIF-2 |
| TFCI-1 | 5 Terraform CI | `terraform-ci.yml` (plan artifact) | WIF-2, ADO-3 |
| TFCI-2 | 5 Terraform CI | Build-validation policy on `main` | TFCI-1 |
| TFCD-1 | 6 Terraform CD | `azure-ai-platform-dev` env + approval | WIF-3 |
| TFCD-2 | 6 Terraform CD | `terraform-cd.yml` (dev apply, smoke) | TFCD-1, TFCI-1 |
| TFCD-3 | 6 Terraform CD | Rollback + state-recovery runbook | TFCD-2 |
| HARD-1 | 7 Backend hardening | Blob data access for identities | WIF-2, WIF-3 |
| HARD-2 | 7 Backend hardening | Backend → Entra auth + verify | HARD-1 |
| HARD-3 | 7 Backend hardening | Disable Shared Key + validate recovery | HARD-2 |
| DBX-1 | 8 Databricks platform | Workspace via Terraform | AZ-1, WIF-3, HARD-2 |
| DBX-2 | 8 Databricks platform | Access connector + Unity Catalog | DBX-1, AZ-3 |
| DBX-3 | 8 Databricks platform | CI/CD SP + ADO↔Databricks WIF | DBX-2, WIF-1 |
| DAB-1 | 9 Databricks CI/CD | Sample bundle (dev/prod targets) | DBX-3 |
| DAB-2 | 9 Databricks CI/CD | Bundle CI/CD via Azure Pipelines | DAB-1, TFCD-1 |
| POC-1 | 10 AI POC | Python 3.12 project (uv/Ruff/mypy/pytest) | BOOT-2 |
| POC-2 | 10 AI POC | Narrow agent + eval + telemetry + guardrails | POC-1 |
| POC-3 | 10 AI POC | Deploy agent via pipeline | POC-2, DAB-2 |
| GH-1 | 11 GitHub comparison | Mirror + GitHub Actions plan-only (OIDC) | Stages 3–10 |
| GH-2 | 11 GitHub comparison | Comparison + platform-decision ADR | GH-1 |
</content>
</invoke>
