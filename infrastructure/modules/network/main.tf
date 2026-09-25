resource "azurerm_virtual_network" "this" {
  name                = "vnet-aiplatform-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = var.address_space

  tags = var.tags
}

resource "azurerm_network_security_group" "databricks" {
  name                = "nsg-dbx-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name

  tags = var.tags
}

resource "azurerm_subnet" "databricks_host" {
  name                 = "snet-dbx-host-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = var.databricks_host_subnet_prefixes

  delegation {
    name = "databricks"

    service_delegation {
      name = "Microsoft.Databricks/workspaces"

      actions = [
        "Microsoft.Network/virtualNetworks/subnets/join/action",
        "Microsoft.Network/virtualNetworks/subnets/prepareNetworkPolicies/action",
        "Microsoft.Network/virtualNetworks/subnets/unprepareNetworkPolicies/action",
      ]
    }
  }
}

resource "azurerm_subnet" "databricks_container" {
  name                 = "snet-dbx-container-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = var.databricks_container_subnet_prefixes

  delegation {
    name = "databricks"

    service_delegation {
      name = "Microsoft.Databricks/workspaces"

      actions = [
        "Microsoft.Network/virtualNetworks/subnets/join/action",
        "Microsoft.Network/virtualNetworks/subnets/prepareNetworkPolicies/action",
        "Microsoft.Network/virtualNetworks/subnets/unprepareNetworkPolicies/action",
      ]
    }
  }
}

resource "azurerm_subnet" "private_endpoints" {
  name                 = "snet-private-endpoints-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = var.private_endpoint_subnet_prefixes
}

resource "azurerm_subnet_network_security_group_association" "host" {
  subnet_id                 = azurerm_subnet.databricks_host.id
  network_security_group_id = azurerm_network_security_group.databricks.id
}

resource "azurerm_subnet_network_security_group_association" "container" {
  subnet_id                 = azurerm_subnet.databricks_container.id
  network_security_group_id = azurerm_network_security_group.databricks.id
}

# --- Egress: NAT gateway + retained public IP ---------------------------------
#
# The public IP is ALWAYS kept. It is a cheap, stable address that external
# allowlists (for example the sandbox Foundry account's IP rules) may depend on,
# so idle mode never releases it. Only the NAT gateway and its two subnet
# associations are conditional: with nat_gateway_enabled = false they are
# destroyed and the public IP is left unattached; setting it back to true
# recreates the gateway and re-associates the SAME address.
resource "azurerm_public_ip" "databricks_nat" {
  name                = "pip-dbx-nat-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  allocation_method   = "Static"
  sku                 = "Standard"

  tags = var.tags
}

resource "azurerm_nat_gateway" "databricks" {
  count = var.nat_gateway_enabled ? 1 : 0

  name                    = "nat-dbx-${var.environment}"
  location                = var.location
  resource_group_name     = var.resource_group_name
  sku_name                = "Standard"
  idle_timeout_in_minutes = 10

  tags = var.tags
}

resource "azurerm_nat_gateway_public_ip_association" "databricks" {
  count = var.nat_gateway_enabled ? 1 : 0

  nat_gateway_id       = azurerm_nat_gateway.databricks[0].id
  public_ip_address_id = azurerm_public_ip.databricks_nat.id
}

resource "azurerm_subnet_nat_gateway_association" "host" {
  count = var.nat_gateway_enabled ? 1 : 0

  subnet_id      = azurerm_subnet.databricks_host.id
  nat_gateway_id = azurerm_nat_gateway.databricks[0].id
}

resource "azurerm_subnet_nat_gateway_association" "container" {
  count = var.nat_gateway_enabled ? 1 : 0

  subnet_id      = azurerm_subnet.databricks_container.id
  nat_gateway_id = azurerm_nat_gateway.databricks[0].id
}

# The NAT resources were unconditional (no count) when every environment was
# first deployed. Introducing `count` changes their addresses from `x.y` to
# `x.y[0]`; these moved blocks carry the existing state entries across so that
# enabling the feature flag in its default (active) position is a pure no-op
# plan — no destroy, no replace, no re-create.
moved {
  from = azurerm_nat_gateway.databricks
  to   = azurerm_nat_gateway.databricks[0]
}

moved {
  from = azurerm_nat_gateway_public_ip_association.databricks
  to   = azurerm_nat_gateway_public_ip_association.databricks[0]
}

moved {
  from = azurerm_subnet_nat_gateway_association.host
  to   = azurerm_subnet_nat_gateway_association.host[0]
}

moved {
  from = azurerm_subnet_nat_gateway_association.container
  to   = azurerm_subnet_nat_gateway_association.container[0]
}
