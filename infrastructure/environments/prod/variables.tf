variable "subscription_id" {
  description = "Azure subscription ID. Supplied at runtime (TF_VAR_subscription_id resolved from the pipeline service connection); never committed."
  type        = string
}

variable "location" {
  description = "Azure region for the prod platform resources."
  type        = string
  default     = "uksouth"
}

variable "environment" {
  description = "Environment name; drives tagging."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["prod"], var.environment)
    error_message = "This root is the prod environment; environment must be \"prod\"."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group. Referenced via data source; Terraform must not create or destroy it."
  type        = string
  default     = "rg-aiplatform-prod"
}

variable "storage_account_name" {
  description = "Globally unique name for the ADLS Gen2 storage account. Confirm global uniqueness before apply."
  type        = string
}

variable "key_vault_name" {
  description = "Globally unique Key Vault name. The logical convention is kv-aiplatform-prod; the physical name carries a stable platform suffix (000000 in this example, as used by the tfstate account) because the bare kv-aiplatform-prod name is not globally available. Max 24 characters."
  type        = string
  default     = "kv-aiplatform-prod000000"
}

variable "log_analytics_workspace_name" {
  description = "Name of the Log Analytics workspace."
  type        = string
  default     = "log-aiplatform-prod"
}

variable "log_analytics_retention_days" {
  description = "Log Analytics retention in days (minimum 30 for the PerGB2018 SKU)."
  type        = number
  default     = 30
}

variable "log_analytics_daily_quota_gb" {
  description = "Daily ingestion cap (GB) as a cost guardrail for a governed environment."
  type        = number
  default     = 1
}

variable "storage_soft_delete_retention_days" {
  description = "Blob/container soft-delete retention (days) for a governed environment."
  type        = number
  default     = 7
}

variable "databricks_workspace_name" {
  description = "Name of the Azure Databricks workspace. Environment-neutral variable name; the prod value is the default."
  type        = string
  default     = "dbw-aiplatform-prod"
}

variable "databricks_access_connector_name" {
  description = "Name of the Azure Databricks Access Connector. Environment-neutral variable name; the prod value is the default."
  type        = string
  default     = "ac-aiplatform-databricks-prod"
}
