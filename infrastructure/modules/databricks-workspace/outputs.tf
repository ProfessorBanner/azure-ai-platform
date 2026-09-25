output "databricks_workspace_id" {
  description = "Azure resource ID of the Databricks workspace."
  value       = azurerm_databricks_workspace.this.id
}

output "databricks_workspace_name" {
  description = "Name of the Databricks workspace."
  value       = azurerm_databricks_workspace.this.name
}

output "databricks_workspace_url" {
  description = "Per-workspace URL (host) of the Databricks workspace."
  value       = azurerm_databricks_workspace.this.workspace_url
}

output "managed_resource_group_id" {
  description = "Resource ID of the Databricks-managed resource group holding the workspace's managed resources."
  value       = azurerm_databricks_workspace.this.managed_resource_group_id
}

output "databricks_workspace_numeric_id" {
  description = "Numeric Databricks workspace ID used by Databricks workspace-level APIs."
  value       = azurerm_databricks_workspace.this.workspace_id
}
