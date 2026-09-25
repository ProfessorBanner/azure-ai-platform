variable "storage_account_name" {
  description = "Globally unique name for the ADLS Gen2 storage account (3-24 lowercase alphanumeric characters)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.storage_account_name))
    error_message = "storage_account_name must be 3-24 characters, lowercase letters and numbers only."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group that will contain the storage account."
  type        = string
}

variable "location" {
  description = "Azure region for the storage account."
  type        = string
}

variable "public_network_access_enabled" {
  description = "Whether the storage account is reachable over its public endpoint. Default-deny; data-plane networking is deferred to a later stage."
  type        = bool
  default     = false
}

variable "blob_soft_delete_retention_days" {
  description = "Retention window (days) for soft-deleted blobs. Sized for a personal dev environment."
  type        = number
  default     = 7

  validation {
    condition     = var.blob_soft_delete_retention_days >= 1 && var.blob_soft_delete_retention_days <= 365
    error_message = "blob_soft_delete_retention_days must be between 1 and 365."
  }
}

variable "container_soft_delete_retention_days" {
  description = "Retention window (days) for soft-deleted containers. Sized for a personal dev environment."
  type        = number
  default     = 7

  validation {
    condition     = var.container_soft_delete_retention_days >= 1 && var.container_soft_delete_retention_days <= 365
    error_message = "container_soft_delete_retention_days must be between 1 and 365."
  }
}

variable "tags" {
  description = "Tags applied to the storage account. Must include the platform tag schema (environment, platform, managed_by, purpose)."
  type        = map(string)
}
