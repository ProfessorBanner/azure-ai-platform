output "resource_group_name" {
  description = "Name of the externally provisioned resource group hosting the platform."
  value       = data.azurerm_resource_group.platform.name
}

output "storage_account_id" {
  description = "Resource ID of the ADLS Gen2 storage account."
  value       = module.storage.storage_account_id
}

output "storage_primary_dfs_endpoint" {
  description = "Primary Data Lake Storage (DFS) endpoint of the storage account."
  value       = module.storage.primary_dfs_endpoint
}

output "storage_account_name" {
  description = "Name of the ADLS Gen2 storage account."
  value       = module.storage.storage_account_name
}

output "key_vault_id" {
  description = "Resource ID of the Key Vault."
  value       = module.key_vault.key_vault_id
}

output "key_vault_uri" {
  description = "Data-plane URI of the Key Vault."
  value       = module.key_vault.key_vault_uri
}

output "log_analytics_workspace_id" {
  description = "Resource ID of the Log Analytics workspace."
  value       = module.log_analytics.workspace_id
}

output "databricks_workspace_id" {
  description = "Resource ID of the Azure Databricks workspace."
  value       = module.databricks_workspace.databricks_workspace_id
}

output "databricks_workspace_name" {
  description = "Name of the Azure Databricks workspace."
  value       = module.databricks_workspace.databricks_workspace_name
}

output "databricks_workspace_url" {
  description = "Per-workspace URL (host) of the Azure Databricks workspace."
  value       = module.databricks_workspace.databricks_workspace_url
}

output "databricks_access_connector_id" {
  description = "Resource ID of the Azure Databricks Access Connector."
  value       = module.databricks_access_connector.databricks_access_connector_id
}

output "databricks_access_connector_name" {
  description = "Name of the Azure Databricks Access Connector."
  value       = module.databricks_access_connector.databricks_access_connector_name
}

output "databricks_access_connector_principal_id" {
  description = "Principal ID of the Access Connector's system-assigned managed identity (no role assigned in Phase 7)."
  value       = module.databricks_access_connector.databricks_access_connector_principal_id
}

# --- Platform mode (additive to the stable contract) -------------------------

output "platform_mode" {
  description = "Effective platform mode of this environment: \"active\" or \"idle\" (see var.idle_mode and docs/runbooks/platform-idle-mode.md)."
  value       = var.idle_mode ? "idle" : "active"
}

output "nat_gateway_id" {
  description = "Resource ID of the Databricks egress NAT gateway; null while idle."
  value       = module.network.nat_gateway_id
}

output "nat_public_ip_address" {
  description = "Static public IP address retained for Databricks egress in both modes."
  value       = module.network.nat_public_ip_address
}

output "storage_dfs_private_endpoint_id" {
  description = "Resource ID of the storage dfs private endpoint; null while idle."
  value       = module.storage_private_access.dfs_private_endpoint_id
}

output "storage_blob_private_endpoint_id" {
  description = "Resource ID of the storage blob private endpoint; null while idle."
  value       = module.storage_private_access.blob_private_endpoint_id
}
