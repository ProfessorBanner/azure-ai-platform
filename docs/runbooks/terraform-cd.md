# Terraform CD runbook (controlled apply)

This runbook documents the controlled Terraform CD pipeline
(`azure-pipelines/terraform-cd.yml`, backlog **TFCD-2**): what it does, the two
identities it uses, how the exact reviewed plan is promoted to apply, the human
approval gate, and how to roll back. It is the operational companion to
`docs/runbooks/terraform-ci.md`, `docs/adr/0003-azure-devops-primary-cicd.md`
and `docs/runbooks/terraform-state-recovery.md`.

> **Current stage.** The expected apply is a **no-op**: the bootstrap state
> backend already exists and matches configuration. This stage exists to prove
> the *controlled CD control path* end-to-end (post-merge plan → approval →
> exact-plan apply → drift check), not to change infrastructure.

## Flow

```
merge to main
     │
     ▼
Validate  ─ fmt-check, tflint, Trivy            ┐
     │                                          │ Stage 1: validate_plan
Plan      ─ terraform plan -out (plan-only WIF) │ identity: sc-azure-terraform-plan
     │      from the exact merged main commit   │
Publish   ─ sanitized plan.txt + logs           │
     │                                          │
Guard     ─ destructive-change check (JSON)     ┘
     │
     ▼
Azure DevOps Environment  azure-ai-platform-dev  ── MANUAL APPROVAL ──┐
     │                                                                │ Stage 2: apply
Apply     ─ terraform apply <exact saved plan> (apply WIF)            │ identity: sc-azure-terraform-apply
     │                                                                │
Post-apply ─ terraform plan -detailed-exitcode (0 = no drift)        ┘
```

## What the pipeline does

| Stage | Step | Command / script | Identity |
|-------|------|------------------|----------|
| validate_plan | Install tooling | `scripts/ci/install-tools.sh` | — |
| validate_plan | Format check | `make fmt-check` | — |
| validate_plan | Lint | `make lint` (`tflint --recursive`) | — |
| validate_plan | Security | `make security` (Trivy) | — |
| validate_plan | Init/validate/plan | `scripts/ci/terraform-plan.sh` (`plan -out=terraform.tfplan`) | **plan** |
| validate_plan | Destructive guard | `scripts/ci/check-destructive-plan.sh` | — (offline) |
| validate_plan | Publish | `terraform-plan` artifact (`plan.txt` + logs) | — |
| validate_plan | Promote binary | `terraform-plan-binary` artifact (binary plan) | — |
| apply | Download plan | `terraform-plan-binary` artifact | — |
| apply | **Approval** | Environment `azure-ai-platform-dev` gate | human |
| apply | Apply | `scripts/ci/terraform-cd-apply.sh` (`apply <plan>`) | **apply** |
| apply | Post-apply drift | `terraform plan -detailed-exitcode` | **apply** |

The pipeline **reuses** the CI scripts (`install-tools.sh`, `terraform-plan.sh`)
so the plan, redaction and OIDC-leak logic are not duplicated. Only the
destructive guard and the apply/drift step are CD-specific.

## Identity separation — plan vs apply

Plan and apply use **different Entra-federated managed identities via separate
service connections**; they are never mixed in one job.

| Concern | `sc-azure-terraform-plan` | `sc-azure-terraform-apply` |
|---------|---------------------------|-----------------------------|
| Used for | Validate + post-merge **plan** | **apply** of the reviewed plan + post-apply drift plan |
| Azure control-plane role | **Reader** at subscription | **Contributor on `rg-aiplatform-bootstrap-dev` only** |
| State data-plane role | Storage Blob Data Contributor (tfstate container) | Storage Blob Data Contributor (tfstate container) |
| Can mutate infrastructure? | **No** | Yes, but **only** within `rg-aiplatform-bootstrap-dev` |
| Owner / User Access Admin / subscription-wide Contributor | Never | Never |

**Why the apply identity is constrained to `rg-aiplatform-bootstrap-dev`:** this
is the blast-radius control. Even after a human approves, the apply identity
physically cannot change anything outside the bootstrap resource group. As later
stages add resource groups, each apply scope is granted deliberately and
reviewably — the identity is never given subscription-wide Contributor.

**Why the plan identity cannot apply:** it holds only Reader + state data
access. Generating the plan and applying it are therefore separated by identity,
not merely by pipeline convention.

## Why CD generates a fresh post-merge plan

CI produces a plan on the PR branch, but CD does **not** reuse it. After merge,
CD checks out the **exact merged `main` commit** and generates a new plan from
it, because:

- the merge commit's tree can differ from any single PR branch (multiple PRs,
  merge resolution, `main` moved on);
- the plan must reflect the state of the world **at apply time**, against
  current remote state, so the approver reviews what will actually happen;
- it keeps a single source of truth: what merged to `main` is what is planned
  and applied.

## Exact-plan promotion (the plan you approve is the plan that applies)

1. Stage 1 runs `terraform plan -out=terraform.tfplan` (via
   `scripts/ci/terraform-plan.sh`) using the **plan** identity.
2. The binary plan is copied to a staging dir **outside**
   `Build.ArtifactStagingDirectory` and published as the internal
   `terraform-plan-binary` pipeline artifact. A sanitized human-readable
   `plan.txt` is published separately as `terraform-plan`.
3. Stage 2 downloads `terraform-plan-binary` and runs
   `terraform apply <that binary plan>` with the **apply** identity.
4. Stage 2 **never re-runs `terraform plan`** before apply. The approved bytes
   are the applied bytes. (A saved plan applies without `-auto-approve` and
   reads its decided variable values from the file.)

The binary plan is a **short-lived internal promotion artifact**, not a
user-facing deliverable. Keep pipeline artifact retention minimal; it exists
only to carry the reviewed plan across the approval boundary.

## The Azure DevOps Environment approval

- Stage 2 is an Azure DevOps **deployment job** bound to the environment
  `azure-ai-platform-dev`.
- That environment already carries a **manual approval** check. Binding to it
  pauses the pipeline until a named human approves.
- This approval **is** the `terraform apply` human-approval control mandated by
  `CLAUDE.md`. There is deliberately **no** YAML `ManualValidation` task — a
  second gate would be redundant and confusing.
- The approver should review the published `plan.txt` (and the guard result)
  before approving, checking for unexpected or costly changes.

## Destructive-change guard (lab guardrail, not a policy engine)

`scripts/ci/check-destructive-plan.sh` decodes the binary plan with
`terraform show -json` and fails the run if any resource change includes a
`delete` action (pure delete or replace). This runs **before** approval so an
approver is never asked to approve a silent deletion.

This is a deliberately small **lab guardrail**. It only inspects the JSON action
list; it does not evaluate cost, data sensitivity, force-replacement nuance, or
module provenance. A production setup should add policy-as-code (OPA/Conftest,
Sentinel, or equivalent) alongside the human approval. The Environment approval
remains the authoritative gate.

## Post-apply drift check

After apply, `scripts/ci/terraform-cd-apply.sh` runs
`terraform plan -detailed-exitcode`:

| Exit code | Meaning | Pipeline result |
|-----------|---------|-----------------|
| `0` | No changes — state matches config | **pass** (expected) |
| `2` | Changes remain after apply (drift) | **fail** — investigate |
| `1` | Plan errored | **fail** |

## Security posture

- **No long-lived credentials.** Each stage requests a fresh OIDC token
  (`AzureCLI@2` + `addSpnToEnvironment`) and maps it to transient `ARM_*`
  variables. No storage account key, SAS token, client secret or PAT is
  created, stored or accepted.
- **Entra state auth.** The backend uses `use_azuread_auth = true`; Shared Key
  is not used by the pipeline even though it remains temporarily enabled on the
  account.
- **Never published:** Terraform state, `.terraform/`, OIDC tokens, Azure CLI
  credentials, environment dumps. The human-readable plan is redacted by
  `terraform-plan.sh` and the workspace is scanned for the OIDC token; the
  promoted binary plan is additionally checked for the token before promotion.
- **No destroy.** Neither script runs `terraform destroy`; apply only ever
  applies a reviewed, non-destructive saved plan (the guard blocks deletes).

## Rollback and state recovery

CD rolls **forward through the same gated path** — it never uses
`terraform destroy` on shared state.

1. **Revert the change** on `main` via a normal reviewed PR (e.g. `git revert`
   the offending merge). The revert merge triggers CD.
2. CD plans the reversal from the merged commit, publishes it, and pauses for
   approval; an approver reviews and approves.
3. Apply applies the reversal with the apply identity; the drift check confirms
   convergence.

If a run is interrupted and leaves a **state lock**, or a bad state write must
be undone, follow `docs/runbooks/terraform-state-recovery.md`: confirm no active
operation owns the lease before any `force-unlock`, and prefer restoring a prior
**blob version** over editing the current state blob. `prevent_destroy` on the
state account and the CanNotDelete lock remain in force throughout.

## Manual Azure DevOps configuration still required

Claude Code authors YAML only; the following are **human** Azure DevOps actions:

1. **Create the pipeline** from YAML: Pipelines → New pipeline → Azure Repos Git
   → this repo → Existing YAML → `/azure-pipelines/terraform-cd.yml`, named
   `terraform-cd`.
2. **Authorize service connections** for the pipeline on first run (or pre-grant
   in each connection's *Security*): `sc-azure-terraform-plan` and
   `sc-azure-terraform-apply`. Restrict `sc-azure-terraform-apply` to the
   `terraform-cd` pipeline only (do not open it to all pipelines).
3. **Confirm the Environment** `azure-ai-platform-dev` exists with a **manual
   approval** check naming a human approver (not the requester). This pipeline
   does not create or modify the environment.
4. **Pipeline artifact retention:** confirm the retention policy keeps the
   `terraform-plan-binary` artifact only as long as needed.
5. Optionally add an **Exclusive lock** check on the environment to serialise
   applies (belt-and-braces with azurerm state locking).

## Failure triage

- **`terraform init` auth failure** — confirm the relevant managed identity
  still holds *Storage Blob Data Contributor* on the tfstate container and the
  federation subject matches this pipeline.
- **Guard fails (destructive change)** — expected only if the plan really would
  delete/replace. Review `plan.txt`; do not bypass the guard to force a delete.
- **Apply blocks on the lock** — a previous run or local operator holds the
  lease; wait for the 300s timeout or recover per the state-recovery runbook.
- **Post-apply drift (exit 2)** — something changed the resources out-of-band,
  or the plan was stale; re-plan and investigate before re-applying.

## Related

- `docs/runbooks/terraform-ci.md` — plan-only CI, shared scripts.
- `docs/runbooks/terraform-state-recovery.md` — lock/state recovery.
- `docs/adr/0002-terraform-state-backend.md` — state backend & Entra auth.
- `docs/adr/0003-azure-devops-primary-cicd.md` — plan/apply split.
- `docs/implementation-backlog.md` — TFCD-1 / TFCD-2 / TFCD-3.
