# Private connectivity for an ADLS Gen2 storage account.
#
# This module describes the storage private-link path proven out manually in the
# SANDBOX runtime test: with the storage account at publicNetworkAccess=Disabled
# and default_action=Deny, Databricks classic compute in the injected VNet can
# only reach ADLS through private endpoints whose FQDNs resolve, via linked
# private DNS zones, to private IPs inside the VNet.
#
# It provisions, for both the `dfs` (ADLS Gen2 / data-lake) and `blob` sub-
# resources of the account:
#   - a Private DNS zone (privatelink.dfs / privatelink.blob .core.windows.net);
#   - a VNet link from that zone to the environment VNet (no auto-registration);
#   - a Private Endpoint in the private-endpoint subnet, auto-approved against
#     the storage account, with a "default" private_dns_zone_group wiring the PE
#     A-record into the matching zone.
#
# The AzureRM provider manages the Private Endpoints and the Private DNS VNet
# links directly; no separate DNS-zone-group or record resources are needed.
#
# Resource labels (.dfs / .blob) and derived names are chosen to match the
# already-created Azure resources exactly, so state adoption is a deterministic
# `terraform import` with no replacement.

locals {
  dfs_private_endpoint_name  = "pe-${var.storage_account_name}-dfs"
  blob_private_endpoint_name = "pe-${var.storage_account_name}-blob"
}

# --- Private DNS zones (global) ---------------------------------------------

resource "azurerm_private_dns_zone" "dfs" {
  name                = "privatelink.dfs.core.windows.net"
  resource_group_name = var.resource_group_name

  tags = var.tags
}

resource "azurerm_private_dns_zone" "blob" {
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = var.resource_group_name

  tags = var.tags
}

# --- VNet links --------------------------------------------------------------

resource "azurerm_private_dns_zone_virtual_network_link" "dfs" {
  name                  = "link-${var.virtual_network_name}-dfs"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.dfs.name
  virtual_network_id    = var.virtual_network_id
  registration_enabled  = false

  tags = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  name                  = "link-${var.virtual_network_name}-blob"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.blob.name
  virtual_network_id    = var.virtual_network_id
  registration_enabled  = false

  tags = var.tags
}

# --- Private endpoints -------------------------------------------------------
#
# Conditional on private_endpoints_enabled. In IDLE mode both endpoints (and,
# with them, their NICs and the "default" private_dns_zone_group) are destroyed.
# Azure removes the A records the zone group registered, so no stale endpoint
# IP is left behind in the zones. The zones and their VNet links above are
# unconditional and always retained; restoration recreates the endpoints, whose
# zone groups re-register fresh A records with whatever IPs the subnet assigns.

resource "azurerm_private_endpoint" "dfs" {
  count = var.private_endpoints_enabled ? 1 : 0

  name                = local.dfs_private_endpoint_name
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "${local.dfs_private_endpoint_name}-connection"
    private_connection_resource_id = var.storage_account_id
    subresource_names              = ["dfs"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.dfs.id]
  }

  tags = var.tags
}

resource "azurerm_private_endpoint" "blob" {
  count = var.private_endpoints_enabled ? 1 : 0

  name                = local.blob_private_endpoint_name
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "${local.blob_private_endpoint_name}-connection"
    private_connection_resource_id = var.storage_account_id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob.id]
  }

  tags = var.tags
}

# The endpoints were unconditional when adopted into state. Adding `count`
# changes their addresses to `[0]`; these moved blocks keep the active
# configuration a no-op plan (no destroy/replace) when the flag is introduced.
moved {
  from = azurerm_private_endpoint.dfs
  to   = azurerm_private_endpoint.dfs[0]
}

moved {
  from = azurerm_private_endpoint.blob
  to   = azurerm_private_endpoint.blob[0]
}
