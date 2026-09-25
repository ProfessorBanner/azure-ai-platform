variable "subscription_id" {
  description = "Azure subscription ID. Supplied at runtime (TF_VAR_subscription_id resolved from the pipeline service connection); never committed."
  type        = string
}

variable "location" {
  description = "Azure region for the sandbox platform resources."
  type        = string
  default     = "uksouth"
}

variable "environment" {
  description = "Environment name; drives tagging. Sandbox is an experimentation environment class, not a governed promotion stage."
  type        = string
  default     = "sandbox"

  validation {
    condition     = contains(["sandbox"], var.environment)
    error_message = "This root is the sandbox environment; environment must be \"sandbox\"."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group. Referenced via data source; Terraform must not create or destroy it."
  type        = string
  default     = "rg-aiplatform-sandbox"
}

variable "storage_account_name" {
  description = "Globally unique name for the ADLS Gen2 storage account. Confirm global uniqueness before apply."
  type        = string
}

variable "key_vault_name" {
  description = "Globally unique Key Vault name. Confirm global uniqueness before apply."
  type        = string
  default     = "kv-aiplatform-sandbox"
}

variable "log_analytics_workspace_name" {
  description = "Name of the Log Analytics workspace."
  type        = string
  default     = "log-aiplatform-sandbox"
}

variable "log_analytics_retention_days" {
  description = "Log Analytics retention in days (minimum 30 for the PerGB2018 SKU)."
  type        = number
  default     = 30
}

variable "log_analytics_daily_quota_gb" {
  description = "Daily ingestion cap (GB) as a cost guardrail. Cheaper sandbox default; governed environments use a higher cap."
  type        = number
  default     = 0.5
}

variable "storage_soft_delete_retention_days" {
  description = "Blob/container soft-delete retention (days). Shorter in sandbox (disposable posture); governed environments retain longer."
  type        = number
  default     = 1
}

variable "databricks_workspace_name" {
  description = "Name of the Azure Databricks workspace. Environment-neutral variable name; the sandbox value is the default."
  type        = string
  default     = "dbw-aiplatform-sandbox"
}

variable "databricks_access_connector_name" {
  description = "Name of the Azure Databricks Access Connector. Environment-neutral variable name; the sandbox value is the default."
  type        = string
  default     = "ac-aiplatform-databricks-sandbox"
}
