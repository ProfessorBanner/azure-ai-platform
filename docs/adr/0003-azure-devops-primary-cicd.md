# ADR 0003: Azure DevOps is the primary source-control and CI/CD platform

## Status

Accepted. Revises the CI/CD and source-control choice in ADR 0001 (which named
GitHub as the initial primary platform). ADR 0001's platform *scope* otherwise
still stands.

The migration to Azure Repos is **complete**: Azure Repos is now the canonical
repository and GitHub is retained only as a temporary backup remote. Migration
coordinates, authentication and branch-policy configuration are recorded in
`docs/runbooks/azure-repos-migration.md`.

## Context

The target enterprise stack this personal platform exists to practise is
**Azure + Azure DevOps + Azure Databricks**. The platform's purpose is to learn
and rehearse patterns that transfer directly to that environment.

ADR 0001 originally chose GitHub as the initial primary source-control and CI/CD
platform, with Azure DevOps introduced later for comparison. That ordering
optimises for familiarity rather than for the enterprise target. Running two
CI/CD platforms in parallel also risks a state where more than one system can
deploy to the same environment, which is both a security and an operational
hazard.

## Decision

1. **Azure DevOps is the primary platform**, chosen because it matches the
   target enterprise stack (Azure, Azure DevOps, Azure Databricks).
2. **Azure Repos is the canonical source repository.** The existing local Git
   history has been migrated into Azure Repos with full history; `main` is the
   protected default branch. **All changes to `main` go through a pull request;
   direct pushes and policy bypasses are prohibited.** Azure DevOps build
   validation is attached to `main` **after** the Terraform CI pipeline exists
   (backlog TFCI-2), not before.
3. **Azure Pipelines owns CI/CD.** Terraform `plan` runs on pull requests;
   Terraform `apply` runs only after merge to `main`. Databricks workloads are
   deployed through Azure Pipelines.
4. **Azure DevOps Environments provide deployment approvals.** The
   `azure-ai-platform-dev` Environment gates `terraform apply` (and any
   production-shaped Databricks deployment) behind explicit human approval.
5. **Workload identity federation is mandatory.** All automated Azure and
   Databricks authentication uses Microsoft Entra-issued workload identity
   federation. Separate `plan` and `apply` service connections are used.
   Deprecated Azure DevOps-issued federation configurations are not used.
6. **Long-lived deployment credentials are prohibited.** No client secrets, no
   storage account keys, and no Databricks personal access tokens are created
   for automated deployment.
7. **Terraform manages durable platform resources** — Azure infrastructure and
   the durable Azure Databricks workspace and governance (Unity Catalog).
8. **Databricks Declarative Automation Bundles manage Databricks workloads** —
   jobs and application code, kept separate from Terraform-managed governance.
9. **GitHub remains only as a temporary backup remote and owns no deployment.**
   A GitHub comparison is deferred to an optional later stage (backlog Stage
   11); if that is ever pursued, GitHub Actions runs **plan-only** validation
   against an isolated `github-lab` state and identity.
10. **No environment may have two deployment owners.** Exactly one CI/CD
    platform owns deployment to any given environment. GitHub is never granted
    apply rights on an Azure DevOps-owned environment.
11. **Claude Code operates locally against the same Git repository.** It creates
    feature branches and pushes them only with explicit human approval, never to
    `main`, never force-pushing, and never bypassing branch policies. Pull-request
    completion is a human, policy-gated action.

## Identity model

- **Application registration** — the Entra application object (global identity
  definition).
- **Service principal** — the tenant-local instance of an application used for
  authorization and role assignments.
- **Managed identity** — an Azure-managed identity with no application object,
  used by Azure resources (e.g. the Databricks access connector) to reach
  storage without keys.
- **Azure DevOps service connection** — the pipeline-facing credential object
  that references an app/SP (or managed identity) and carries the Entra-issued
  federation subject; it holds no secret.

Two ARM service connections are used: `sc-terraform-plan` (Reader at the
required scope + Storage Blob Data Contributor at the state-container scope) and
`sc-terraform-apply` (Contributor at the target resource-group scope only +
state-container Blob data access). The apply identity is never granted Owner or
User Access Administrator, and never subscription-wide Contributor.

## Consequences

### Positive

- Practises the exact enterprise stack (Azure DevOps + Azure + Databricks).
- One deployment owner per environment eliminates a class of conflicts.
- No long-lived credentials to leak, rotate, or audit.
- Clear separation: Terraform for durable platform, Bundles for workloads.
- Approvals are enforced by Azure DevOps Environments, not convention.

### Negative

- GitHub comparison is deferred, so the two platforms are exercised in sequence
  rather than side by side.
- Entra-issued federation for Azure DevOps service connections and for
  Databricks must be configured carefully against current Microsoft guidance.
- Some Azure DevOps configuration (org, project, service connections, branch
  policies, environments) is portal/CLI-driven rather than fully IaC.

### Superseded / revised

- Supersedes the CI/CD-platform ordering in ADR 0001 (GitHub-first). GitHub is
  now an optional, later, plan-only comparison rather than the initial primary
  platform.
</content>
