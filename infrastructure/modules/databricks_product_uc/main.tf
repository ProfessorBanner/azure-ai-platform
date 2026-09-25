# Product-owned Unity Catalog schema — reusable, environment-neutral module.
#
# Creates ONE schema inside an existing, platform-owned environment catalog and
# grants the product's runtime principal the least privilege its batch workload
# actually needs. The catalog, its storage root, the storage credential, the
# external location and the workspace bindings are platform-owned and are
# neither created nor modified here (see docs/platform/product-contract.md).
#
# Grants use `databricks_grant` (singular), NOT `databricks_grants`. The plural
# resource is authoritative for a securable: applying it would delete every
# grant on that catalog or schema it does not list, including grants made by
# other products or by the platform. The singular resource manages only this
# principal's privileges on the securable and leaves unrelated grants intact —
# the difference matters most on the shared environment catalog, where other
# products' USE_CATALOG grants must survive this module's apply.

resource "databricks_schema" "this" {
  catalog_name = var.catalog_name
  name         = var.schema_name

  comment = var.comment
}

# Catalog traversal only. USE_CATALOG confers no read, write or create right on
# anything inside the catalog; it is the minimum needed to reach the schema
# below. Deliberately NOT granted: CREATE_SCHEMA (the product may not create
# further namespaces), ALL_PRIVILEGES, or catalog ownership.
resource "databricks_grant" "catalog_use" {
  catalog = var.catalog_name

  principal  = var.runtime_principal
  privileges = ["USE_CATALOG"]
}

# Schema privileges for the current batch workload: traverse the schema, create
# its own tables, and read and write those tables. Deliberately NOT granted:
# CREATE_FUNCTION (no such workload yet — add it when a UDF is actually
# deployed, not in anticipation), CREATE_SCHEMA, ALL_PRIVILEGES, or schema
# ownership.
#
# CREATE_MODEL is added only when the product opts in via enable_create_model,
# which an ML product that registers a model in the Unity Catalog Model Registry
# must do. It is a single additional privilege on this one schema: it confers no
# right to create models anywhere else, and no right to create further
# namespaces. A product whose workload registers no model keeps the original
# privilege set unchanged, because the default is false.
locals {
  schema_workload_privileges = concat(
    [
      "USE_SCHEMA",
      "CREATE_TABLE",
      "SELECT",
      "MODIFY",
    ],
    var.enable_create_model ? ["CREATE_MODEL"] : [],
  )
}

resource "databricks_grant" "schema_workload" {
  schema = databricks_schema.this.id

  principal  = var.runtime_principal
  privileges = local.schema_workload_privileges
}
