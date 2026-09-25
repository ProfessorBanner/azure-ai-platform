output "workspace_id" {
  description = "Resource ID of the Log Analytics workspace."
  value       = azurerm_log_analytics_workspace.this.id
}

output "workspace_customer_id" {
  description = "Workspace (customer) ID GUID used by agents and data sources."
  value       = azurerm_log_analytics_workspace.this.workspace_id
}
