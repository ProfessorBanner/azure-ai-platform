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
  # This changes only how the provider authenticates to Storage; it does not
  # alter the account's security posture, RBAC, service connections or backend.
  storage_use_azuread = true
}

# AzAPI drives the ARM management plane only (no Storage data-plane), so it
# authenticates through the same Microsoft Entra ID / workload identity
# federation path as AzureRM and never needs data-plane network access to the
# private storage account. No extra configuration is required.
provider "azapi" {}

# Workspace-level Databricks provider for DEV Unity Catalog objects. The host is
# derived from the Terraform-managed workspace output (no duplicated literal
# URL). Authentication uses Databricks unified auth — no PAT, OAuth or client
# secret is embedded here. Local plans supply DATABRICKS_CONFIG_PROFILE
# (e.g. aiplatform-dev) via the environment; CI auth is handled separately and
# must not introduce long-lived secrets.
provider "databricks" {
  host = "https://${module.databricks_workspace.databricks_workspace_url}"
}

data "azurerm_client_config" "current" {}
