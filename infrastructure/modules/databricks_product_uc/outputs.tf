output "schema_id" {
  description = "Unity Catalog identifier of the product schema, in <catalog>.<schema> form."
  value       = databricks_schema.this.id
}

output "schema_name" {
  description = "Name of the product schema, without the catalog prefix."
  value       = databricks_schema.this.name
}
