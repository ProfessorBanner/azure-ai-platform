variable "resource_group_name" {
  description = "Name of the externally provisioned resource group that contains the storage account, VNet and private-endpoint subnet."
  type        = string
}

variable "location" {
  description = "Azure region for the private endpoints. Private DNS zones are global and ignore this value."
  type        = string
}

variable "storage_account_id" {
  description = "Resource ID of the ADLS Gen2 storage account the private endpoints target."
  type        = string
}

variable "storage_account_name" {
  description = "Name of the ADLS Gen2 storage account. Used to derive private-endpoint names (pe-<name>-dfs / pe-<name>-blob)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.storage_account_name))
    error_message = "storage_account_name must be 3-24 characters, lowercase letters and numbers only."
  }
}

variable "virtual_network_id" {
  description = "Resource ID of the VNet linked to the private DNS zones."
  type        = string
}

variable "virtual_network_name" {
  description = "Name of the VNet. Used to derive DNS VNet link names (link-<vnet>-dfs / link-<vnet>-blob)."
  type        = string
}

variable "private_endpoint_subnet_id" {
  description = "Resource ID of the subnet in which the storage private endpoints are placed."
  type        = string
}

variable "tags" {
  description = "Tags applied to the private endpoints and DNS resources. Should include the platform tag schema (environment, platform, managed_by, purpose)."
  type        = map(string)
  default     = {}
}

variable "private_endpoints_enabled" {
  description = "Whether the dfs/blob private endpoints exist. Set to false by an environment root in IDLE mode: the endpoints, their NICs, their DNS zone groups and the A records those groups own are removed (endpoints bill hourly). The private DNS zones and VNet links are always retained. The storage account's own public-network-access and firewall settings are never changed by this flag."
  type        = bool
  default     = true
}
