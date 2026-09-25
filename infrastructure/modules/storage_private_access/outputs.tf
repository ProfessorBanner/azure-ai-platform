output "dfs_private_dns_zone_id" {
  description = "Resource ID of the privatelink.dfs.core.windows.net private DNS zone."
  value       = azurerm_private_dns_zone.dfs.id
}

output "blob_private_dns_zone_id" {
  description = "Resource ID of the privatelink.blob.core.windows.net private DNS zone."
  value       = azurerm_private_dns_zone.blob.id
}

output "dfs_private_endpoint_id" {
  description = "Resource ID of the dfs (ADLS Gen2) private endpoint, or null when private_endpoints_enabled = false (idle mode)."
  value       = one(azurerm_private_endpoint.dfs[*].id)
}

output "blob_private_endpoint_id" {
  description = "Resource ID of the blob private endpoint, or null when private_endpoints_enabled = false (idle mode)."
  value       = one(azurerm_private_endpoint.blob[*].id)
}

output "dfs_private_endpoint_ip" {
  description = "Private IP allocated to the dfs private endpoint NIC, or null in idle mode."
  value       = one(azurerm_private_endpoint.dfs[*].private_service_connection[0].private_ip_address)
}

output "blob_private_endpoint_ip" {
  description = "Private IP allocated to the blob private endpoint NIC, or null in idle mode."
  value       = one(azurerm_private_endpoint.blob[*].private_service_connection[0].private_ip_address)
}

output "private_endpoints_enabled" {
  description = "Effective private-endpoint switch (true = active, false = idle)."
  value       = var.private_endpoints_enabled
}
