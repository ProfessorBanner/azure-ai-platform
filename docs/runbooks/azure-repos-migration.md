# Azure Repos migration runbook

This runbook records the completed migration of the platform's canonical source
control from GitHub to Azure Repos, and the resulting remote topology,
authentication approach and branch-policy configuration. It is the operational
companion to `docs/adr/0003-azure-devops-primary-cicd.md`.

Status: **migration complete.** Azure Repos is the canonical repository; GitHub
is retained only as a temporary backup remote and owns no deployment.

## Coordinates

| Item | Value |
|------|-------|
| Azure DevOps organisation | `example-org` (`https://dev.azure.com/example-org`) |
| Project | `azure-ai-platform` |
| Repository | `azure-ai-platform` |
| Canonical remote name | `azure` (`origin` is also set to the same Azure Repos URL) |
| Canonical remote URL | `https://example-org@dev.azure.com/example-org/azure-ai-platform/_git/azure-ai-platform` |
| Temporary GitHub backup remote | `github` → `https://github.com/example-user/azure-ai-platform.git` |

Confirm the live configuration at any time with:

```
git remote -v
```

## Authentication approach

- Human Git access authenticates through **Microsoft Entra ID** via Git
  Credential Manager against the Azure DevOps organisation's backing tenant.
- **No personal access tokens (PATs)** are created or stored for either Git
  access or, later, pipeline deployment.
- **No client secrets** are used. When Azure Pipelines is introduced it will use
  Microsoft Entra **workload identity federation** exclusively (see ADR 0003 and
  backlog Stage 4).
- The GitHub backup remote is push-only mirroring for redundancy; it holds no
  deployment credentials and grants no apply rights to any environment.

## Branch-policy configuration

`main` is the protected default branch. The enforced policy is:

- **All changes to `main` go through a pull request.** Direct pushes to `main`
  are blocked for every contributor.
- **Policy bypass is prohibited.** The "Bypass policies when pushing" and
  "Bypass policies when completing pull requests" permissions are denied for all
  non-administrator identities; force-push (history rewrite) is denied on `main`.
- **At least one reviewer** is required where the licence tier allows, with
  "reset votes on new changes" and self-approval prohibited to compensate for
  the single-maintainer constraint.
- Comment resolution is required before completion.
- A **build validation** policy slot is reserved for the Terraform CI pipeline
  and will be attached **after** that pipeline exists (backlog TFCI-2). It is not
  pointed at a non-existent pipeline in the interim.

Read back the active policies (read-only) with:

```
az repos policy list \
  --org https://dev.azure.com/example-org \
  --project azure-ai-platform \
  --repository-id <repository-id>
```

## Claude Code operating model

Claude Code continues to operate **locally against the same Git repository**. It
creates feature branches, commits, and — only with explicit human approval —
pushes a branch to the `azure` remote. It never pushes to `main`, never
force-pushes, and never bypasses branch policies. Pull-request completion is a
human, policy-gated action in Azure DevOps.

## Rollback

The GitHub remote is additive and untouched by the cut-over, so it remains a
full mirror of history. If Azure Repos had to be re-established, the repository
can be re-pushed from any local clone or from the GitHub backup; local history
remains the source of truth until a cut-over is re-confirmed.
