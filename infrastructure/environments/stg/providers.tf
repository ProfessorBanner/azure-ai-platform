terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.127"
    }
  }
}

provider "azurerm" {
  features {}

  subscription_id = var.subscription_id

  # The platform storage account has Shared Key disabled
  # (shared_access_key_enabled = false), so AzureRM must authenticate Storage
  # data-plane operations with Microsoft Entra ID rather than an account key.
  # Without this, data-plane reads fail with 403 KeyBasedAuthenticationNotPermitted.
  storage_use_azuread = true
}

provider "azapi" {}

provider "databricks" {
  host = "https://${module.databricks_workspace.databricks_workspace_url}"
}

data "azurerm_client_config" "current" {}
