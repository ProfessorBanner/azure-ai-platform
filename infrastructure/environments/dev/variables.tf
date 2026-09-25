variable "subscription_id" {
  description = "Azure subscription ID. Supplied at runtime (TF_VAR_subscription_id resolved from the pipeline service connection); never committed."
  type        = string
}

variable "location" {
  description = "Azure region for the dev platform resources."
  type        = string
  default     = "uksouth"
}

variable "environment" {
  description = "Environment name; drives tagging."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "Only the dev environment is supported in this stage."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group. Referenced via data source; Terraform must not create or destroy it."
  type        = string
  default     = "rg-aiplatform-dev"
}

variable "storage_account_name" {
  description = "Globally unique name for the ADLS Gen2 storage account. Confirm global uniqueness before apply."
  type        = string
}

variable "key_vault_name" {
  description = "Globally unique Key Vault name. Confirm global uniqueness before apply."
  type        = string
  default     = "kv-aiplatform-dev"
}

variable "log_analytics_workspace_name" {
  description = "Name of the Log Analytics workspace."
  type        = string
  default     = "log-aiplatform-dev"
}

variable "log_analytics_retention_days" {
  description = "Log Analytics retention in days (minimum 30 for the PerGB2018 SKU)."
  type        = number
  default     = 30
}

variable "databricks_workspace_name" {
  description = "Name of the Azure Databricks workspace. Environment-neutral variable name; the dev value is the default."
  type        = string
  default     = "dbw-aiplatform-dev"
}

variable "databricks_access_connector_name" {
  description = "Name of the Azure Databricks Access Connector. Environment-neutral variable name; the dev value is the default."
  type        = string
  default     = "ac-aiplatform-databricks-dev"
}
