output "resource_group_name" {
  description = "Bootstrap resource group name."
  value       = azurerm_resource_group.bootstrap.name
}

output "storage_account_name" {
  description = "Terraform state storage account name."
  value       = azurerm_storage_account.terraform_state.name
}

output "storage_account_id" {
  description = "Terraform state storage account resource ID."
  value       = azurerm_storage_account.terraform_state.id
}

output "container_name" {
  description = "Terraform state container name."
  value       = azurerm_storage_container.terraform_state.name
}

output "current_principal_object_id" {
  description = "Object ID of the identity executing Terraform."
  value       = data.azurerm_client_config.current.object_id
}
