variable "workspace_name" {
  description = "Name of the Log Analytics workspace (unique within the resource group)."
  type        = string
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group that will contain the workspace."
  type        = string
}

variable "location" {
  description = "Azure region for the workspace."
  type        = string
}

variable "retention_in_days" {
  description = "Log retention in days. Short but reasonable for a personal dev platform (minimum 30 for the PerGB2018 SKU)."
  type        = number
  default     = 30

  validation {
    condition     = var.retention_in_days >= 30 && var.retention_in_days <= 730
    error_message = "retention_in_days must be between 30 and 730."
  }
}

variable "daily_quota_gb" {
  description = "Daily ingestion cap (GB) as a cost guardrail. -1 means unbounded; a small positive value bounds spend on a personal platform."
  type        = number
  default     = 1

  validation {
    condition     = var.daily_quota_gb == -1 || var.daily_quota_gb > 0
    error_message = "daily_quota_gb must be -1 (unbounded) or a positive number."
  }
}

variable "tags" {
  description = "Tags applied to the workspace. Must include the platform tag schema (environment, platform, managed_by, purpose)."
  type        = map(string)
}
