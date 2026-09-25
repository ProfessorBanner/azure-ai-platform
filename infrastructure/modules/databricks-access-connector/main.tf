# Azure Databricks Access Connector — reusable, environment-neutral module.
#
# The access connector holds a system-assigned managed identity that Unity
# Catalog (Phase 8) will use to reach Azure Data Lake storage WITHOUT any storage
# account key or secret. One connector per environment.
#
# Phase 7 provisions ONLY the connector and its identity. NO Azure role
# assignments are created here: the connector is deliberately inert until Phase 8
# wires it to storage, because no Unity Catalog storage credential or external
# location consumes its identity yet. Creating the RBAC now would grant standing
# storage access to an unused identity — so identity creation and permission
# grant are kept as separate, independently reviewed changes (see ADR 0005).
resource "azurerm_databricks_access_connector" "this" {
  name                = var.databricks_access_connector_name
  resource_group_name = var.resource_group_name
  location            = var.location

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}
