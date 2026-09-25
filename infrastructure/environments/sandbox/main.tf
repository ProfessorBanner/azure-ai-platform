# Sandbox platform root (state key: platform/sandbox.tfstate).
#
# SANDBOX is a separate environment CLASS for human experimentation and
# interactive development — disposable data, weaker durability, no downstream
# guarantees, no production credentials. It is NOT part of the governed
# dev -> stg -> prod promotion chain. Useful sandbox work is committed to Git and
# then flows through the governed lifecycle (feature -> PR -> main -> DEV -> ...).
#
# It composes the SAME reusable modules as every other environment (no sandbox
# forks): storage, Key Vault, Log Analytics, a Premium Databricks workspace, and
# a Databricks Access Connector. The only differences are configuration values
# (cheaper cost caps, shorter retention) and tags supplied from this root.

locals {
  # Common tag schema plus a sandbox-specific disposable-lifecycle marker. Each
  # module also receives a resource-specific `purpose`.
  common_tags = {
    environment = var.environment
    platform    = "azure-ai-platform"
    managed_by  = "terraform"
    lifecycle   = "disposable"
  }
}

# rg-aiplatform-sandbox is provisioned OUTSIDE this configuration (externally
# bootstrapped). Referenced by data source so Terraform never creates or
# destroys the environment resource group.
data "azurerm_resource_group" "platform" {
  name = var.resource_group_name
}

module "storage" {
  source = "../../modules/storage"

  storage_account_name                 = var.storage_account_name
  resource_group_name                  = data.azurerm_resource_group.platform.name
  location                             = var.location
  blob_soft_delete_retention_days      = var.storage_soft_delete_retention_days
  container_soft_delete_retention_days = var.storage_soft_delete_retention_days
  tags                                 = merge(local.common_tags, { purpose = "data-lake" })
}

module "key_vault" {
  source = "../../modules/key-vault"

  key_vault_name      = var.key_vault_name
  resource_group_name = data.azurerm_resource_group.platform.name
  location            = var.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  tags                = merge(local.common_tags, { purpose = "platform-secrets" })
}

module "log_analytics" {
  source = "../../modules/log-analytics"

  workspace_name      = var.log_analytics_workspace_name
  resource_group_name = data.azurerm_resource_group.platform.name
  location            = var.location
  retention_in_days   = var.log_analytics_retention_days
  daily_quota_gb      = var.log_analytics_daily_quota_gb
  tags                = merge(local.common_tags, { purpose = "platform-observability" })
}

module "databricks_workspace" {
  source = "../../modules/databricks-workspace"

  databricks_workspace_name = var.databricks_workspace_name
  resource_group_name       = data.azurerm_resource_group.platform.name
  location                  = var.location

  virtual_network_id  = module.network.virtual_network_id
  public_subnet_name  = module.network.databricks_host_subnet_name
  private_subnet_name = module.network.databricks_container_subnet_name

  public_subnet_network_security_group_association_id  = module.network.databricks_host_nsg_association_id
  private_subnet_network_security_group_association_id = module.network.databricks_container_nsg_association_id

  tags = merge(local.common_tags, { purpose = "databricks-workspace" })
}

module "databricks_access_connector" {
  source = "../../modules/databricks-access-connector"

  databricks_access_connector_name = var.databricks_access_connector_name
  resource_group_name              = data.azurerm_resource_group.platform.name
  location                         = var.location
  tags                             = merge(local.common_tags, { purpose = "databricks-access-connector" })
}

# --- Phase 8 network foundation ---------------------------------------------

module "network" {
  source = "../../modules/network"

  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.platform.name
  location            = var.location

  # Idle mode removes the NAT gateway + associations; the public IP is retained.
  nat_gateway_enabled = !var.idle_mode

  address_space = [
    "10.10.0.0/24"
  ]

  databricks_host_subnet_prefixes = [
    "10.10.0.0/26"
  ]

  databricks_container_subnet_prefixes = [
    "10.10.0.64/26"
  ]

  private_endpoint_subnet_prefixes = [
    "10.10.0.128/27"
  ]

  tags = merge(local.common_tags, {
    purpose = "platform-network"
  })
}

# --- Phase 9C storage private connectivity ----------------------------------
#
# Adopts the SANDBOX storage private-link path proven in the Phase 9C runtime
# test: with stexamplesandboxdata at publicNetworkAccess=Disabled +
# default_action=Deny, Databricks classic compute reaches ADLS only through
# these private endpoints and linked private DNS zones. The resources already
# exist in Azure (created manually) and are imported into state in a separate
# manual step; this configuration describes them for ongoing management.
module "storage_private_access" {
  source = "../../modules/storage_private_access"

  resource_group_name  = data.azurerm_resource_group.platform.name
  location             = var.location
  storage_account_id   = module.storage.storage_account_id
  storage_account_name = module.storage.storage_account_name

  virtual_network_id         = module.network.virtual_network_id
  virtual_network_name       = module.network.virtual_network_name
  private_endpoint_subnet_id = module.network.private_endpoint_subnet_id

  # Idle mode removes the dfs/blob private endpoints; zones and links are retained.
  private_endpoints_enabled = !var.idle_mode

  tags = merge(local.common_tags, { purpose = "storage-private-access" })
}
