variable "catalog_name" {
  description = "Name of the existing environment catalog the product schema is created in. The catalog itself is platform-owned and is never created or modified by this module."
  type        = string
}

variable "schema_name" {
  description = "Name of the product-owned Unity Catalog schema, in Databricks-compatible snake_case (see docs/platform/naming-standard.md)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]*$", var.schema_name))
    error_message = "schema_name must be lowercase snake_case: start with a letter, then letters, digits or underscores."
  }
}

variable "runtime_principal" {
  description = "Application (client) ID of the product's runtime/deployment service principal. This principal receives the least-privilege grants below; it is granted nothing at metastore, storage-credential or external-location level."
  type        = string
}

variable "comment" {
  description = "Comment recorded on the schema, describing which product owns it."
  type        = string
  default     = null
}

variable "enable_create_model" {
  description = "Whether the runtime principal may create Unity Catalog registered models in this schema. Adds CREATE_MODEL to the schema privileges and nothing else; false (the default) preserves the original batch-workload privilege set exactly."
  type        = bool
  default     = false
}
