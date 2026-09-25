# Terraform CI runbook (plan only)

This runbook documents the plan-only Terraform CI pipeline
(`azure-pipelines/terraform-ci.yml`, backlog **TFCI-1**): what it does, the
identity and permissions it uses, why it can never apply, and how the
pull-request build-validation policy is configured. It is the operational
companion to `docs/adr/0003-azure-devops-primary-cicd.md` and
`docs/adr/0002-terraform-state-backend.md`.

## What the pipeline does

On a feature-branch push (and on pull requests, via branch policy), a single
Microsoft-hosted **Ubuntu** job runs the full non-destructive check sequence,
reusing the repository `Makefile` targets where they are safe and deterministic:

| Step | Command | Purpose |
|------|---------|---------|
| Install tooling | `scripts/ci/install-tools.sh` | Pin & verify Terraform, TFLint, Trivy |
| Format check | `make fmt-check` (`terraform fmt -check -recursive`) | Formatting gate |
| Lint | `make lint` (`tflint --recursive`) | Terraform lint |
| Security | `make security` (`trivy fs --scanners vuln,secret,misconfig`) | IaC/secret/vuln scan, fail on HIGH/CRITICAL |
| Init | `terraform init` against `backend/dev.hcl` | Remote state, Entra auth |
| Validate | `terraform validate` | Configuration validity |
| Plan | `terraform plan -out=terraform.tfplan` | Read-only plan |
| Render | `terraform show -no-color` → redacted `plan.txt` | Human-readable plan |
| Publish | `terraform-plan` artifact | `plan.txt` + validation logs only |

Pinned versions live in `azure-pipelines/terraform-ci.yml` (`terraformVersion`,
`tflintVersion`, `trivyVersion`). The Terraform version is pinned explicitly so
plans are reproducible across runs.

### What is (and is not) published

The `terraform-plan` artifact contains **only** the redacted human-readable
`plan.txt` and the validation logs (`logs/*.log`). The pipeline never publishes:

- Terraform state (`*.tfstate`);
- the **binary** plan (`terraform.tfplan`) — it stays in the workspace only;
- `.terraform/` contents or provider binaries;
- OIDC tokens or any credential.

`scripts/ci/terraform-plan.sh` redacts sensitive-looking assignments from the
plan and then greps every staged file for the OIDC token, failing the run and
wiping the staging directory if the token is ever found.

## Identity and permissions

The only Azure-authenticated step is an `AzureCLI@2` task bound to the
service connection **`sc-aiplatform-tf-plan`**.

- **Type:** Azure Resource Manager, **workload identity federation** backed by a
  **user-assigned managed identity**. No client secret exists.
- **Roles (least privilege):**
  - **Reader** at **subscription** scope — read-only; cannot mutate resources.
  - **Storage Blob Data Contributor** scoped to the **tfstate container** only —
    Entra data-plane access to read/write the state blob and its lock. This is
    *not* Shared Key: the backend uses `use_azuread_auth = true`.
- The identity has **no Contributor, Owner or User Access Administrator** role,
  so it is structurally incapable of applying infrastructure changes.

### Fresh OIDC, no long-lived credentials

`addSpnToEnvironment: true` makes the `AzureCLI@2` task perform a **fresh Azure
DevOps OIDC token request per run** and expose `servicePrincipalId`, `idToken`
and `tenantId` to the script. `scripts/ci/terraform-plan.sh` maps those into the
only Terraform variables required, all transient to the step:

```
ARM_USE_OIDC=true
ARM_USE_AZUREAD=true
ARM_CLIENT_ID=<managed identity client id>
ARM_TENANT_ID=<tenant id>
ARM_SUBSCRIPTION_ID=<subscription bound to the connection>
ARM_OIDC_TOKEN=<short-lived OIDC token>   # never echoed
```

No PAT, client secret, storage account key or SAS token is created, stored or
accepted anywhere in the pipeline.

## Why `apply` is prohibited

1. **Policy:** `CLAUDE.md` lists `terraform apply` as a human-approval-only
   command; CI runs unattended and must not perform it.
2. **Separation of duties:** plan (read-only) and apply (scoped Contributor) use
   **different** identities and different pipelines (ADR 0003). CI uses the plan
   identity exclusively.
3. **Least privilege enforces it:** even if an `apply` step were added by
   mistake, `sc-aiplatform-tf-plan` lacks any write role and the apply would
   fail. Apply happens only in the CD pipeline (backlog Stage 6), gated by an
   Azure DevOps Environment approval.

There is deliberately **no `terraform apply` or `terraform destroy` step** in
`terraform-ci.yml`, `templates/terraform-checks.yml`, or the CI scripts.

## Concurrency

- The CI trigger uses `batch: true` so queued runs on the same branch are
  serialised rather than run in parallel.
- `terraform plan` uses `-lock-timeout=120s`; the azurerm backend takes a blob
  lease lock, so two runs cannot plan against the same state simultaneously.
- **Stronger option (optional):** attach an Azure DevOps Environment with an
  *Exclusive lock* check to serialise all Terraform runs org-wide. Not required
  for the current single-maintainer volume.

## How PR build validation is configured (manual, Azure DevOps)

The pipeline intentionally sets `pr: none`. Pull-request validation is driven by
an Azure Repos **branch policy**, not a YAML PR trigger. Configure it once
(backlog **TFCI-2**):

1. First create the pipeline from YAML: **Pipelines → New pipeline → Azure
   Repos Git →** select this repo **→ Existing Azure Pipelines YAML file →**
   `/azure-pipelines/terraform-ci.yml`. Name it `terraform-ci`.
2. Authorise the pipeline to use the `sc-aiplatform-tf-plan` service connection
   on first run (or pre-grant it in the connection's *Security* settings).
3. **Project settings → Repositories →** *azure-ai-platform* **→ Policies →**
   branch **`main`** **→ Build Validation → +**:
   - Build pipeline: `terraform-ci`;
   - Trigger: **Automatic**;
   - Policy requirement: **Required**;
   - Build expiration: **Immediately when `main` is updated** (or after ~12h);
   - Path filter: optional (e.g. `bootstrap/*;backend/*;azure-pipelines/*`).
4. Verify a PR cannot complete while the check is failing or stale.

Read back the active policy (read-only):

```
az repos policy list \
  --org https://dev.azure.com/example-org \
  --project azure-ai-platform \
  --repository-id <repository-id>
```

> Creating/authorising the pipeline and adding the build-validation policy are
> **manual Azure DevOps actions** — they are not performed by Claude Code and
> require a human in the Azure DevOps UI/CLI.

## Failure triage

- **`terraform init` fails on auth** — confirm the managed identity still holds
  *Storage Blob Data Contributor* on the **tfstate container** and that the
  federation subject on the service connection matches this pipeline.
- **`terraform plan` blocks on the lock** — a previous run or a local operator
  holds the state lease; wait for the 120s timeout or recover per
  `docs/runbooks/terraform-state-recovery.md` (never blindly force-unlock).
- **Trivy fails on HIGH/CRITICAL** — fix the finding; do not lower the severity
  gate to pass CI.
- **Tool download fails** — the pinned release URL/version is unreachable; check
  the version variables in `terraform-ci.yml`.

## Related

- `docs/adr/0002-terraform-state-backend.md` — state backend & Entra auth.
- `docs/adr/0003-azure-devops-primary-cicd.md` — plan/apply split, CI on PRs.
- `docs/runbooks/terraform-state-recovery.md` — lock/state recovery.
- `docs/implementation-backlog.md` — TFCI-1 / TFCI-2.
