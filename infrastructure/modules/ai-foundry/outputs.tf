# Operational outputs. No key, token or other secret is exposed: the account runs
# with local_auth_enabled = false, so no secret material exists to output.

output "account_id" {
  description = "Resource ID of the Foundry (AIServices cognitive) account."
  value       = azurerm_cognitive_account.this.id
}

output "account_name" {
  description = "Name of the Foundry account."
  value       = azurerm_cognitive_account.this.name
}

output "account_endpoint" {
  description = "Data-plane endpoint of the Foundry account."
  value       = azurerm_cognitive_account.this.endpoint
}

output "account_custom_subdomain_name" {
  description = "Custom subdomain of the account; the label used to build data-plane hostnames."
  value       = azurerm_cognitive_account.this.custom_subdomain_name
}

output "account_principal_id" {
  description = "Principal ID of the account's system-assigned managed identity."
  value       = one(azurerm_cognitive_account.this.identity[*].principal_id)
}

output "project_id" {
  description = "Resource ID of the Foundry project."
  value       = azurerm_cognitive_account_project.this.id
}

output "project_name" {
  description = "Name of the Foundry project."
  value       = azurerm_cognitive_account_project.this.name
}

output "project_principal_id" {
  description = "Principal ID of the project's system-assigned managed identity."
  value       = one(azurerm_cognitive_account_project.this.identity[*].principal_id)
}

output "project_endpoints" {
  description = "Map of endpoints published by the Foundry project, keyed by API surface. Resolved by Azure at apply time."
  value       = azurerm_cognitive_account_project.this.endpoints
}

output "deployment_name" {
  description = "Name of the model deployment; the identifier a client sends as its model."
  value       = azurerm_cognitive_deployment.this.name
}

output "model_name" {
  description = "Name of the deployed model."
  value       = one(azurerm_cognitive_deployment.this.model[*].name)
}

output "model_version" {
  description = "Pinned version of the deployed model."
  value       = one(azurerm_cognitive_deployment.this.model[*].version)
}

output "model_format" {
  description = "Publisher format of the deployed model."
  value       = one(azurerm_cognitive_deployment.this.model[*].format)
}

output "deployment_sku_name" {
  description = "SKU of the model deployment."
  value       = one(azurerm_cognitive_deployment.this.sku[*].name)
}

output "deployment_capacity" {
  description = "Provisioned capacity of the model deployment, in units of 1,000 tokens per minute."
  value       = one(azurerm_cognitive_deployment.this.sku[*].capacity)
}

output "version_upgrade_option" {
  description = "How Azure handles new model versions for this deployment."
  value       = azurerm_cognitive_deployment.this.version_upgrade_option
}

output "diagnostic_setting_id" {
  description = "Resource ID of the diagnostic setting streaming account telemetry to Log Analytics."
  value       = azurerm_monitor_diagnostic_setting.this.id
}

output "local_auth_enabled" {
  description = "Whether API-key authentication is permitted on the account. False means Entra ID is the only accepted credential."
  value       = azurerm_cognitive_account.this.local_auth_enabled
}

output "public_network_access_enabled" {
  description = "Whether the account exposes a public endpoint."
  value       = azurerm_cognitive_account.this.public_network_access_enabled
}

output "network_acls_default_action" {
  description = "Default action of the account network ACL."
  value       = one(azurerm_cognitive_account.this.network_acls[*].default_action)
}
