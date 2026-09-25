output "databricks_access_connector_id" {
  description = "Azure resource ID of the Databricks Access Connector."
  value       = azurerm_databricks_access_connector.this.id
}

output "databricks_access_connector_name" {
  description = "Name of the Databricks Access Connector."
  value       = azurerm_databricks_access_connector.this.name
}

output "databricks_access_connector_principal_id" {
  description = "Principal (object) ID of the access connector's system-assigned managed identity. Consumed by the Phase 8 Unity Catalog storage RBAC grant; no role is assigned in Phase 7."
  value       = azurerm_databricks_access_connector.this.identity[0].principal_id
}
