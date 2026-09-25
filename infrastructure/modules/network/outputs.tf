output "virtual_network_id" {
  description = "Resource ID of the environment VNet."
  value       = azurerm_virtual_network.this.id
}

output "virtual_network_name" {
  description = "Name of the environment VNet."
  value       = azurerm_virtual_network.this.name
}

output "databricks_host_subnet_id" {
  description = "Resource ID of the Databricks host subnet."
  value       = azurerm_subnet.databricks_host.id
}

output "databricks_host_subnet_name" {
  description = "Name of the Databricks host subnet."
  value       = azurerm_subnet.databricks_host.name
}

output "databricks_container_subnet_id" {
  description = "Resource ID of the Databricks container subnet."
  value       = azurerm_subnet.databricks_container.id
}

output "databricks_container_subnet_name" {
  description = "Name of the Databricks container subnet."
  value       = azurerm_subnet.databricks_container.name
}

output "private_endpoint_subnet_id" {
  description = "Resource ID of the subnet reserved for Private Endpoints."
  value       = azurerm_subnet.private_endpoints.id
}

output "databricks_nsg_id" {
  description = "Resource ID of the NSG associated with Databricks subnets."
  value       = azurerm_network_security_group.databricks.id
}

output "databricks_host_nsg_association_id" {
  value = azurerm_subnet_network_security_group_association.host.id
}

output "databricks_container_nsg_association_id" {
  value = azurerm_subnet_network_security_group_association.container.id
}

output "nat_gateway_id" {
  description = "Resource ID of the Databricks egress NAT gateway, or null when nat_gateway_enabled = false (idle mode)."
  value       = one(azurerm_nat_gateway.databricks[*].id)
}

output "nat_public_ip_id" {
  description = "Resource ID of the retained NAT public IP (exists in both active and idle mode)."
  value       = azurerm_public_ip.databricks_nat.id
}

output "nat_public_ip_address" {
  description = "Static public IPv4 address of the retained NAT public IP. Stable across idle/active transitions."
  value       = azurerm_public_ip.databricks_nat.ip_address
}

output "nat_gateway_enabled" {
  description = "Effective NAT gateway switch (true = active egress, false = idle)."
  value       = var.nat_gateway_enabled
}
