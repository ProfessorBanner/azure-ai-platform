# ML/AI Platform Naming Standard

## Principles

Names should be:

- deterministic
- environment-aware where required
- product-oriented
- compatible with Azure, Databricks and Azure DevOps constraints
- predictable enough for automation

## Product

Canonical product identifier:

```text
<product-name>
```

Use lowercase kebab-case for repository and deployment-facing product identifiers.

Examples:

```text
demand-forecasting
ai-optimiser
b2b-recommender
```

## Repository

```text
products/<product-name>/
```

## Environments

Canonical governed environments:

```text
dev
stg
prod
```

Sandbox is outside the governed promotion chain.

## Databricks jobs

```text
<product-name>-<environment>
```

Examples:

```text
demand-forecasting-dev
demand-forecasting-stg
demand-forecasting-prod
```

## Catalogs

Current platform environment catalogs:

```text
dev
stg
prod
```

Products must not create environment-specific catalogs themselves.

## Schemas

Prefer product- or capability-oriented schemas when isolation is required:

```text
<product_identifier>
```

Use Databricks-compatible snake_case.

Examples:

```text
demand_forecasting
ai_optimiser
```

Shared schemas such as `platform_test` are platform-owned exceptions.

## Models

Unity Catalog model names use the three-level namespace:

```text
<catalog>.<schema>.<model_name>
```

Example:

```text
prod.demand_forecasting.demand_model
```

## Experiments

Use the product and experiment intent:

```text
/<product-name>/<experiment-name>
```

The detailed MLflow convention will be defined in Phase 15.

## Deployment service principals

The platform deployment identity convention is:

```text
sp-aiplatform-dbx-deploy-<environment>
```

Product-specific deployment identities may extend this convention when introduced.

## Azure DevOps pipelines

Prefer:

```text
aiplatform-<capability>-<environment>-<purpose>
```

Examples:

```text
aiplatform-dbx-dev-cd
aiplatform-dbx-stg-cd
aiplatform-dbx-prod-cd
```

## Rules

Do not:

- encode mutable resource IDs in names
- encode Git SHAs in permanent resource names
- create environment-specific source forks
- invent product-local environment abbreviations
- duplicate authoritative IDs in documentation
