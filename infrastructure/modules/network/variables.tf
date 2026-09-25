variable "environment" {
  description = "Environment name used for resource naming and tagging."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group where networking resources are created."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
}

variable "address_space" {
  description = "Address space assigned to the environment VNet."
  type        = list(string)
}

variable "databricks_host_subnet_prefixes" {
  description = "CIDR prefixes for the Databricks host/public subnet."
  type        = list(string)
}

variable "databricks_container_subnet_prefixes" {
  description = "CIDR prefixes for the Databricks container/private subnet."
  type        = list(string)
}

variable "private_endpoint_subnet_prefixes" {
  description = "CIDR prefixes for Azure Private Endpoints."
  type        = list(string)
}

variable "tags" {
  description = "Tags applied to network resources."
  type        = map(string)
  default     = {}
}

variable "nat_gateway_enabled" {
  description = "Whether the Databricks egress NAT gateway and its subnet/public-IP associations exist. Set to false by an environment root in IDLE mode: the gateway and associations are destroyed (they bill hourly), while the public IP resource and its address are always retained so external allowlists stay valid and restoration re-attaches the same address."
  type        = bool
  default     = true
}
