variable "account_name" {
  description = "Name of the Microsoft Foundry (AIServices cognitive) account, unique within the resource group."
  type        = string
}

variable "custom_subdomain_name" {
  description = "GLOBALLY UNIQUE custom subdomain for the account. Required for Microsoft Entra ID token authentication and for network ACLs; forms the data-plane hostname."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.custom_subdomain_name))
    error_message = "custom_subdomain_name must be 3-63 lowercase alphanumeric or hyphen characters and must not start or end with a hyphen."
  }
}

variable "account_sku_name" {
  description = "SKU of the AIServices account. S0 is the only SKU offered for this kind in UK South."
  type        = string
  default     = "S0"

  validation {
    condition     = contains(["S0"], var.account_sku_name)
    error_message = "account_sku_name must be \"S0\"; no other SKU is offered for kind AIServices."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group that will contain the Foundry account. This module never creates a resource group."
  type        = string
}

variable "location" {
  description = "Azure region for the Foundry account, project and deployment."
  type        = string
}

variable "public_network_access_enabled" {
  description = "Whether the account exposes a public endpoint. Phase 16 uses a public endpoint fenced by a default-deny IP allow-list; private endpoints are out of scope."
  type        = bool
  default     = true
}

variable "network_acls_default_action" {
  description = "Default action of the account network ACL. Must be Deny so that only explicitly allowed CIDRs can reach the data plane."
  type        = string
  default     = "Deny"

  validation {
    condition     = contains(["Deny", "Allow"], var.network_acls_default_action)
    error_message = "network_acls_default_action must be \"Deny\" or \"Allow\"; \"Deny\" is the required platform posture."
  }
}

variable "allowed_ip_cidrs" {
  description = "IPv4 addresses or CIDR ranges permitted to reach the account data plane. Supplied by the caller at runtime; never committed. /31 and /32 prefixes are normalised to a bare address because Cognitive Services rejects host-sized prefixes."
  type        = list(string)

  validation {
    condition = alltrue([
      for cidr in var.allowed_ip_cidrs :
      can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}(/([0-9]|[12][0-9]|3[0-2]))?$", cidr))
    ])
    error_message = "allowed_ip_cidrs entries must be IPv4 addresses or IPv4 CIDR ranges."
  }

  validation {
    condition     = length(var.allowed_ip_cidrs) > 0
    error_message = "allowed_ip_cidrs must contain at least one entry; a default-deny account with no allowed CIDR is unreachable."
  }
}

variable "project_name" {
  description = "Name of the Foundry project hosted by the account."
  type        = string
}

variable "project_display_name" {
  description = "Human-readable display name of the Foundry project."
  type        = string
}

variable "project_description" {
  description = "Description of the Foundry project's purpose."
  type        = string
}

variable "deployment_name" {
  description = "Name of the model deployment. This is the identifier a client sends as its model, so it is part of the Phase 17 consumer contract and must be stable."
  type        = string
}

variable "model_format" {
  description = "Publisher format of the deployed model."
  type        = string
  default     = "OpenAI"
}

variable "model_name" {
  description = "Name of the model to deploy."
  type        = string
}

variable "model_version" {
  description = "Pinned model version. Left explicit so the served model never changes implicitly."
  type        = string
}

variable "deployment_sku_name" {
  description = "Deployment SKU. \"Standard\" is the regional SKU, which keeps inference within the account's region; \"GlobalStandard\" routes across Microsoft's global fleet."
  type        = string

  validation {
    condition     = contains(["Standard", "GlobalStandard", "DataZoneStandard"], var.deployment_sku_name)
    error_message = "deployment_sku_name must be Standard, GlobalStandard or DataZoneStandard."
  }
}

variable "deployment_capacity" {
  description = "Deployment capacity in units of 1,000 tokens per minute. Kept minimal as a cost and blast-radius guardrail."
  type        = number

  validation {
    condition     = var.deployment_capacity > 0
    error_message = "deployment_capacity must be greater than zero."
  }
}

variable "version_upgrade_option" {
  description = "How Azure handles new model versions. NoAutoUpgrade pins the served version for reproducibility."
  type        = string
  default     = "NoAutoUpgrade"

  validation {
    condition     = contains(["NoAutoUpgrade", "OnceNewDefaultVersionAvailable", "OnceCurrentVersionExpired"], var.version_upgrade_option)
    error_message = "version_upgrade_option must be NoAutoUpgrade, OnceNewDefaultVersionAvailable or OnceCurrentVersionExpired."
  }
}

variable "log_analytics_workspace_id" {
  description = "Resource ID of the EXISTING Log Analytics workspace that receives account diagnostics. This module never creates a workspace."
  type        = string
}

variable "diagnostic_setting_name" {
  description = "Name of the diagnostic setting attached to the Foundry account."
  type        = string
  default     = "diag-to-log-analytics"
}

variable "tags" {
  description = "Tags applied to the account and project. Must include the platform tag schema (environment, platform, managed_by, purpose)."
  type        = map(string)
}
