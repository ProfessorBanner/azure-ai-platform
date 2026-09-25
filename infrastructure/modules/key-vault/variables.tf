variable "key_vault_name" {
  description = "Globally unique Key Vault name (3-24 alphanumeric characters and hyphens, starting with a letter)."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$", var.key_vault_name))
    error_message = "key_vault_name must be 3-24 characters, alphanumeric and hyphens, start with a letter and end with a letter or digit."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group that will contain the Key Vault."
  type        = string
}

variable "location" {
  description = "Azure region for the Key Vault."
  type        = string
}

variable "tenant_id" {
  description = "Microsoft Entra tenant ID used for Key Vault RBAC authorization."
  type        = string
}

variable "soft_delete_retention_days" {
  description = "Soft-delete retention window (days) for the Key Vault. Sized for a personal dev environment."
  type        = number
  default     = 7

  validation {
    condition     = var.soft_delete_retention_days >= 7 && var.soft_delete_retention_days <= 90
    error_message = "soft_delete_retention_days must be between 7 and 90."
  }
}

variable "public_network_access_enabled" {
  description = "Whether the Key Vault is reachable over its public endpoint. Temporarily true for this dev lab; tighten when networking is designed."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to the Key Vault. Must include the platform tag schema (environment, platform, managed_by, purpose)."
  type        = map(string)
}
