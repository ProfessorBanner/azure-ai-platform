variable "databricks_access_connector_name" {
  description = "Name of the Azure Databricks Access Connector (3-64 characters: letters, numbers, hyphens and underscores). Supplied by the environment root; contains no environment-specific literal here."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9-_]{3,64}$", var.databricks_access_connector_name))
    error_message = "databricks_access_connector_name must be 3-64 characters, using letters, numbers, hyphens and underscores only."
  }
}

variable "resource_group_name" {
  description = "Name of the externally provisioned resource group that will contain the access connector."
  type        = string
}

variable "location" {
  description = "Azure region for the access connector."
  type        = string
}

variable "tags" {
  description = "Tags applied to the access connector. Must include the platform tag schema (environment, platform, managed_by, purpose)."
  type        = map(string)
}
