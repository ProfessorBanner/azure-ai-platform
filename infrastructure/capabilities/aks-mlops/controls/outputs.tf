output "subscription_id" {
  description = "The exact subscription the controls are pinned to."
  value       = var.subscription_id
}

output "session_resource_group_id" {
  description = "Resource ID of the empty, allowlisted session group. The session root deploys into it; the watchdog may empty it."
  value       = azurerm_resource_group.session.id
}

output "session_resource_group_name" {
  description = "Name of the session resource group (input to the session root)."
  value       = azurerm_resource_group.session.name
}

output "session_node_resource_group_name" {
  description = "Name the session root must request for the AKS node resource group."
  value       = var.session_node_resource_group_name
}

output "watchdog_principal_id" {
  description = "Object ID of the Automation account's system-assigned managed identity."
  value       = azurerm_automation_account.watchdog.identity[0].principal_id
}

output "watchdog_automation_account_name" {
  description = "Automation account hosting the expiry watchdog."
  value       = azurerm_automation_account.watchdog.name
}

output "watchdog_runbook_name" {
  description = "Runbook name; G4 starts it manually (report mode) to verify execution before arming a real session."
  value       = azurerm_automation_runbook.session_expiry.name
}

output "watchdog_mode" {
  description = "Mode both schedules pass to the runbook. Report until G4 proves the report-only path live."
  value       = var.watchdog_mode
}

output "armed_session" {
  description = "The armed session (null when nothing is armed)."
  value       = var.armed_session
}

output "retained_storage_account_id" {
  description = "Resource ID of the retained artifact storage account (input to the session root for the kubelet read role)."
  value       = azurerm_storage_account.retained.id
}

output "retained_storage_account_name" {
  description = "Name of the retained artifact storage account."
  value       = azurerm_storage_account.retained.name
}

output "retained_containers" {
  description = "Container names by purpose."
  value = {
    inputs  = azurerm_storage_container.inputs.name
    models  = azurerm_storage_container.models.name
    results = azurerm_storage_container.results.name
  }
}

output "session_janitor_role_definition_id" {
  description = "Resource ID of the custom delete-only role the watchdog holds on the session group."
  value       = azurerm_role_definition.session_janitor.role_definition_resource_id
}

output "budget_policy_version" {
  description = "Version of the budget policy this configuration was validated against."
  value       = var.budget_policy.version
}
