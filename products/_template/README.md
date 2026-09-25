# ML/AI Product Template

This directory defines the canonical repository structure for governed ML/AI products.

A product should normally have:

```text
products/<product-name>/
├── databricks.yml
├── resources/
├── src/
├── tests/
│   └── unit/
├── pyproject.toml
└── README.md
```

## Ownership

Product teams own:

- product source code
- tests
- Databricks Bundle resources
- jobs and workflows
- product-specific runtime configuration
- application validation
- product documentation

The platform team owns shared infrastructure, environment foundations, deployment controls and reusable standards.

Environment-specific workspace hosts, catalogs, schemas, compute policies and identities must be injected through Bundle targets or pipeline configuration rather than hard-coded.

Ownership

Product teams own:

product source code
tests
Databricks Bundle resources
jobs and workflows
application-specific runtime configuration
product documentation

The platform owns:

Azure infrastructure
Databricks workspaces
networking and private connectivity
Unity Catalog foundation
compute policies
deployment identity architecture
promotion controls
shared CI/CD primitives

Environment-specific values must be injected through the governed configuration model rather than hard-coded into product source.

The canonical governed lifecycle is:

DEV → STG → PROD

with the same source/SHA promoted between environments.

See:

docs/platform/product-contract.md
docs/platform/current-state.md
docs/platform/engineering-workflow.md

This directory is a structural reference. Do not deploy it as a product.
