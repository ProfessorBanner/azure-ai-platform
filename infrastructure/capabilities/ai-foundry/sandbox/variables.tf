variable "subscription_id" {
  description = "Azure subscription ID. Supplied at runtime (TF_VAR_subscription_id resolved from the pipeline service connection); never committed."
  type        = string
}

variable "location" {
  description = "Azure region for the Foundry capability lab. UK South is the platform default region and offers the regional Standard SKU for the selected model."
  type        = string
  default     = "uksouth"
}

variable "environment" {
  description = "Environment name; drives tagging. The Foundry capability lab exists only in sandbox."
  type        = string
  default     = "sandbox"

  validation {
    condition     = contains(["sandbox"], var.environment)
    error_message = "This capability root is sandbox-only; environment must be \"sandbox\"."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned sandbox resource group. Referenced via data source; Terraform must not create or destroy it."
  type        = string
  default     = "rg-aiplatform-sandbox"
}

variable "log_analytics_workspace_name" {
  description = "Name of the EXISTING sandbox Log Analytics workspace that receives Foundry diagnostics. Owned by platform/sandbox.tfstate and read here by data source."
  type        = string
  default     = "log-aiplatform-sandbox"
}

variable "foundry_account_name" {
  description = "Name of the Microsoft Foundry (AIServices) account."
  type        = string
  default     = "aif-example-sandbox"
}

variable "foundry_custom_subdomain_name" {
  description = "GLOBALLY UNIQUE custom subdomain for the Foundry account. Confirm availability before apply; add a suffix if taken."
  type        = string
  default     = "aif-example-sandbox"
}

variable "allowed_ip_cidrs" {
  description = "IPv4 addresses or CIDR ranges permitted to reach the Foundry data plane. Supplied at runtime (TF_VAR_allowed_ip_cidrs or a git-ignored terraform.tfvars); no address is committed to this repository."
  type        = list(string)
}

variable "foundry_project_name" {
  description = "Name of the Foundry project."
  type        = string
  default     = "proj-capability-lab"
}

variable "foundry_project_display_name" {
  description = "Human-readable display name of the Foundry project."
  type        = string
  default     = "Foundry Capability Lab"
}

variable "foundry_project_description" {
  description = "Description of the Foundry project's purpose."
  type        = string
  default     = "Phase 16 sandbox exploration of Microsoft Foundry: keyless inference, telemetry and evaluation."
}

variable "deployment_name" {
  description = "Name of the model deployment. Part of the Phase 17 consumer contract; changing it is a breaking change for consumers."
  type        = string
  default     = "gpt-4-1-mini"
}

variable "model_name" {
  description = "Name of the model to deploy."
  type        = string
  default     = "gpt-4.1-mini"
}

variable "model_version" {
  description = "Pinned model version."
  type        = string
  default     = "2025-04-14"
}

variable "deployment_sku_name" {
  description = "Deployment SKU. Regional Standard keeps inference within UK South; gpt-4.1-mini is the smallest current chat model offering it there."
  type        = string
  default     = "Standard"
}

variable "deployment_capacity" {
  description = "Deployment capacity in units of 1,000 tokens per minute. Raised from 1 to 10 in Phase 16.3A after measurement; see the comment below."
  type        = number

  # Capacity 1 was measured, not guessed, and it is unusable for evaluation.
  # At capacity 1 Azure enforces 1 request per 60 seconds and 1,000 tokens per
  # 60 seconds. One risk-assessment call consumes roughly 547 tokens, so a
  # 36-attempt run returned 1 valid output and 35 rate-limited failures: the
  # deployment was measuring its own throttle rather than the model.
  #
  # 10 gives 10 requests and 10,000 tokens per 60 seconds — roughly 18 calls'
  # worth of tokens, so the request limit binds first and pacing stays simple.
  # It remains a small fraction of the regional allowance (1 of 200 thousand
  # TPM in use), so this consumes no scarce quota, and spend is still bounded:
  # the SKU is pay-per-token, and capacity caps the rate, not the bill.
  default = 10
}

variable "version_upgrade_option" {
  description = "How Azure handles new model versions. Pinned so the served version never changes implicitly."
  type        = string
  default     = "NoAutoUpgrade"
}

# --- Container registry for the Phase 19.1e Hosted Agent ---------------------

variable "container_registry_name" {
  description = "Override for the container registry name. Leave null to derive a deterministic, globally-unique-by-construction name. ACR names are 5-50 lowercase alphanumeric characters with no separators."
  type        = string
  default     = null

  validation {
    condition     = var.container_registry_name == null || can(regex("^[a-z0-9]{5,50}$", var.container_registry_name))
    error_message = "container_registry_name must be 5-50 lowercase alphanumeric characters."
  }
}

variable "container_registry_public_access_enabled" {
  description = "Whether the registry data plane accepts public traffic. TRUE for this sandbox proof so a workstation and the Foundry runtime can push and pull without private networking; a private endpoint is the durable answer and is out of scope for Phase 19.1e."
  type        = bool
  default     = true
}

variable "container_registry_role_assignment_mode" {
  description = "ACR permission model. AbacRepositoryPermissions enables repository-scoped ABAC conditions and is preferred; LegacyRegistryPermissions is the registry-wide RBAC model. The pull role granted to the Foundry project is derived from this, so the two cannot drift apart."
  type        = string
  default     = "AbacRepositoryPermissions"

  validation {
    condition     = contains(["AbacRepositoryPermissions", "LegacyRegistryPermissions"], var.container_registry_role_assignment_mode)
    error_message = "container_registry_role_assignment_mode must be AbacRepositoryPermissions or LegacyRegistryPermissions."
  }
}
