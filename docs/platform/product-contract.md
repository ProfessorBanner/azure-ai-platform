# Product Contract

The standard for onboarding an ML/AI product onto this platform: who owns what,
what a product must declare, how it reaches production, and the invariants it may
not break. `hello-databricks` is the current reference implementation of this
contract, not its definition — anything specific to that product is called out as
an example, never as a requirement.

Read `docs/platform/current-state.md` for what the platform currently provides
and `docs/platform/engineering-workflow.md` for how changes to it are made.

## 1. Ownership boundaries

Three owners, no overlap. If a thing appears in two columns, the boundary is
wrong and must be fixed before the product ships.

### Platform-owned

Durable infrastructure, managed in Terraform under `infrastructure/`, changed
only through the platform's own governed CD path:

- Azure subscriptions and resource groups
- Networking: VNets, delegated subnets, NSGs, NAT, route/egress posture
- Databricks workspaces
- ADLS Gen2 accounts, private endpoints, private DNS zones and links
- The Unity Catalog foundation: storage credentials, external locations,
  environment catalogs, workspace bindings
- Compute policies
- The OIDC / workload-identity-federation architecture
- Azure DevOps pipeline templates and shared CI/CD structure
- Environment promotion controls and the gates that enforce them
- Observability and cost foundations (Log Analytics, quotas, tagging)

A product never provisions, modifies or duplicates anything in this list.

### Product-owned

Everything inside `products/<product-name>/`, changed by the product team
through normal PR review:

- Product source code
- Tests: unit, integration, and later evaluation
- The Databricks Asset Bundle definition
- Jobs, workflows and task DAGs
- Models, prompts and agent definitions (as they arrive)
- Product schemas and tables **within the granted catalog/schema boundary**
- Runtime configuration and job parameters
- Application-specific validation and fixtures
- Product documentation

### Environment-owned

Values that differ per environment. Supplied to the product as configuration —
never embedded in product logic:

- Workspace host
- Catalog
- Schema bindings
- Compute policy IDs
- Run-as / deployment identity
- Environment-specific permissions and grants
- DEV / STG / PROD approval and promotion policy

The seam between product-owned and environment-owned is the Bundle target block:
the product declares *that* it needs a catalog; the environment decides *which*.

## 2. The Product entity

Every product declares the following. Where the value is environment-owned, the
product declares the requirement and the platform supplies the value.

| Field | Meaning | Owner |
|---|---|---|
| **Name** | Stable identifier; matches the directory and the Bundle `name` | Product |
| **Owner / team** | Accountable humans, reachable | Product |
| **Source location** | `products/<product-name>/` in this repository | Product |
| **Bundle** | `databricks.yml` with one target per governed environment | Product |
| **Workloads / jobs** | Job and task definitions under `resources/` | Product |
| **Catalog / schema requirements** | What namespaces it needs and why | Product declares, environment grants |
| **Compute class / policy** | Compute shape required; the policy ID enforcing it | Product declares, environment supplies |
| **Deployment identity** | Service principal that deploys and runs it | Environment |
| **CI/CD** | Which CI gates apply and which CD pipelines deploy it | Platform |
| **Monitoring ownership** | Who watches its runs and responds to failure | Product |
| **Lifecycle metadata** | Owner, environment and expiry tags; retirement plan | Product |

A product that cannot fill in every row is not ready to onboard.

## 3. Governed lifecycle

```
source → CI → DEV → validation → STG → validation → PROD approval → PROD
```

- **Source** — a feature branch, reviewed and merged to `main` by PR. The merge
  is the human authorisation for the whole downstream rollout.
- **CI** — deterministic quality gates: formatting, linting, type checking, unit
  tests, and Bundle validation. No deployment.
- **DEV** — first governed deployment. Bundle validate → deploy.
- **Validation** — the product's own acceptance evidence at the lowest
  sufficient proof level (see the workflow document's L0–L5).
- **STG** — triggered only by DEV completion on `main`, gated on same-SHA
  provenance. Validate → plan → deploy → run.
- **Validation** — as above, in a governed environment that mirrors PROD.
- **PROD approval** — the Azure DevOps Environment approval. The human gate.
- **PROD** — validate → plan → deploy → run, then verify the result.

Each promotion step re-verifies its own provenance and fails closed. A product
does not implement these gates; it inherits them.

## 4. Invariants

Non-negotiable. A change that breaks one of these is rejected regardless of
how convenient it is.

1. **Same source, same SHA** is promoted across environments. A commit is
   deployed to PROD, or it is not deployed at all.
2. **No environment-specific source forks.** One code path, parameterised.
3. **Product code must not provision platform networking.** No VNets, subnets,
   NSGs, private endpoints or DNS from a product.
4. **Product code must not own workspace infrastructure.** No workspaces,
   access connectors, storage accounts or metastore-level objects.
5. **Terraform owns durable platform and Unity Catalog infrastructure.**
6. **Bundles own Databricks application and runtime resources** — jobs, tasks,
   workflows, and the product assets they deploy.
7. **Environment values are injected, never hard-coded.** Catalogs, schemas,
   hosts, policy IDs and identities arrive as Bundle variables or job
   parameters, and the code reads them from configuration.
8. **PROD remains approval-gated.** No product may bypass, automate or
   self-approve the production gate.
9. **Product runtime identities use least privilege** — scoped to the granted
   catalog and schema, with no standing platform permissions.
10. **SANDBOX is outside the governed chain.** Sandbox work is experimentation;
    it reaches production only by being committed and promoted like any other
    change.

## 5. Canonical repository shape

```
products/<product-name>/
├── databricks.yml      # Bundle: name, variables, one target per environment
├── resources/          # Job/workflow definitions included by databricks.yml
├── src/                # Product source, deployed and executed by the jobs
├── tests/              # unit/ (+ integration/, evaluation/ as they arrive)
├── pyproject.toml      # Product dependencies and tool configuration
└── README.md           # Purpose, owner, workloads, how to validate
```

Conventions that make the shape work:

- `databricks.yml` declares Bundle **variables** for every environment-owned
  value and sets them per target. No environment literal appears in `src/`.
- `resources/` holds one file per logical workload. Shared compute is declared
  once at job level and referenced by key, so a multi-task DAG does not start
  multiple clusters.
- `src/` reads configuration from the environment (job parameters or task
  environment variables) with the governed default being the least-privileged
  environment.
- `tests/unit/` must run with no cloud credentials and no compute.
- `README.md` states the owner and the acceptance evidence for the product.

### Reference implementation and its gaps

`products/hello-databricks/` demonstrates the Bundle structure, per-target
variables (`catalog`, `schema`, `job_compute_policy_id`), a shared job cluster
referenced by two tasks with an explicit dependency, and configuration read from
the environment rather than hard-coded.

It does **not** yet satisfy the canonical shape: it has no `pyproject.toml` and
no `README.md`, relying on the repository-root `pyproject.toml` for its
dependencies and tooling. New products should follow the shape above; whether to
retrofit the reference implementation is a separate decision, recorded here so
the discrepancy is not mistaken for guidance.

## 6. Onboarding checklist

- [ ] Product entity (§2) fully specified, including named owners
- [ ] Directory follows the canonical shape (§5)
- [ ] Catalog and schema requirements agreed; grants provisioned by the platform
      in Terraform, not by the product
- [ ] Compute requirement agreed and an environment compute policy assigned
- [ ] Deployment identity provisioned per environment, least-privileged, OIDC —
      no PAT, client secret or storage key
- [ ] Bundle targets defined for every governed environment, with all
      environment values as variables
- [ ] Unit tests run with no credentials and no compute
- [ ] CI gates pass; Bundle validates against every target
- [ ] Promotion path exercised DEV → STG before requesting PROD approval
- [ ] Monitoring owner named; lifecycle tags applied
