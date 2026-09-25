# ADR 0001: Build a small production-shaped personal AI platform

## Status

Accepted. The CI/CD and source-control decision below (GitHub-first) is
superseded by ADR 0003, which makes Azure DevOps the primary platform and
defers GitHub to an optional comparison stage. The rest of this ADR's scope
still stands.

## Context

The platform is intended for personal experimentation with Azure,
Terraform, Azure Databricks, CI/CD, AI proofs of concept and agentic
applications.

A complete enterprise landing zone would add considerable complexity before
a working AI application exists.

## Decision

Build a minimal production-shaped platform with:

- one Azure tenant;
- one Azure subscription initially;
- resource-group-level environment separation;
- GitHub as the initial primary source-control and CI/CD platform;
- Terraform for durable cloud infrastructure;
- Databricks Declarative Automation Bundles for Databricks workloads;
- Microsoft Entra workload identity federation;
- human approval for infrastructure deployment;
- explicit budgets, tags and expiry metadata.

Azure DevOps will be introduced after the GitHub deployment path works.

## Consequences

### Positive

- Lower implementation complexity.
- Lower initial cost.
- Faster path to an operational AI application.
- Core enterprise engineering practices are still exercised.

### Negative

- It will not initially demonstrate full enterprise landing-zone design.
- GitHub and Azure DevOps equivalence will be implemented later.
- Production-grade networking will be introduced only when justified.
