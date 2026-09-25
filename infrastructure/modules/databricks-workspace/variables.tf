variable "databricks_workspace_name" {
  description = "Name of the Azure Databricks workspace (3-64 characters: letters, numbers, hyphens and underscores). Supplied by the environment root; contains no environment-specific literal here."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9-_]{3,64}$", var.databricks_workspace_name))
    error_message = "databricks_workspace_name must be 3-64 characters, using letters, numbers, hyphens and underscores only."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group that will contain the workspace."
  type        = string
}

variable "location" {
  description = "Azure region for the workspace."
  type        = string
}

variable "sku" {
  description = "Databricks workspace pricing tier. Premium is required for Unity Catalog and role-based access controls."
  type        = string
  default     = "premium"

  validation {
    condition     = contains(["standard", "premium", "trial"], var.sku)
    error_message = "sku must be one of: standard, premium, trial."
  }
}

variable "public_network_access_enabled" {
  description = "Whether the workspace control plane (web UI and REST API) is reachable over its public endpoint. Kept true in Phase 7 because no Private Link / VNet injection is provisioned yet; tighten when platform networking lands (Phase 8+)."
  type        = bool
  default     = true
}

variable "no_public_ip" {
  description = "Enable Secure Cluster Connectivity (no public IP) so compute nodes in the Databricks-managed VNet have no public IP addresses. Set at creation because changing it later forces workspace replacement."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to the workspace. Must include the platform tag schema (environment, platform, managed_by, purpose)."
  type        = map(string)
}

variable "virtual_network_id" {
  type    = string
  default = null
}

variable "public_subnet_name" {
  type    = string
  default = null
}

variable "private_subnet_name" {
  type    = string
  default = null
}

variable "public_subnet_network_security_group_association_id" {
  type    = string
  default = null
}

variable "private_subnet_network_security_group_association_id" {
  type    = string
  default = null
}
