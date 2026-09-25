# Staging (stg) platform root (state key: platform/stg.tfstate).
#
# STG is the second governed deployment environment in the dev -> stg -> prod
# promotion chain. It composes the SAME reusable modules as every other
# environment (no forks): storage, Key Vault, Log Analytics, a Premium Databricks
# workspace, and a Databricks Access Connector, into the externally provisioned
# rg-aiplatform-stg (referenced by data source, never created or destroyed).

locals {
  common_tags = {
    environment = var.environment
    platform    = "azure-ai-platform"
    managed_by  = "terraform"
  }
}

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
    "10.30.0.0/24"
  ]

  databricks_host_subnet_prefixes = [
    "10.30.0.0/26"
  ]

  databricks_container_subnet_prefixes = [
    "10.30.0.64/26"
  ]

  private_endpoint_subnet_prefixes = [
    "10.30.0.128/27"
  ]

  tags = merge(local.common_tags, {
    purpose = "platform-network"
  })
}

# --- Phase 11.3 STG storage private connectivity ----------------------------

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

# --- Phase 11.4 Unity Catalog storage foundation ----------------------------

resource "azurerm_role_assignment" "databricks_access_connector_storage" {
  scope                = module.storage.storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.databricks_access_connector.databricks_access_connector_principal_id
}

resource "azapi_resource" "ucroot" {
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01"
  name      = "ucroot"
  parent_id = "${module.storage.storage_account_id}/blobServices/default"

  body = {
    properties = {
      publicAccess = "None"
    }
  }
}

# --- Phase 11.5 STG Unity Catalog foundation -------------------------------

resource "databricks_storage_credential" "stg" {
  name = "sc-aiplatform-stg"

  azure_managed_identity {
    access_connector_id = module.databricks_access_connector.databricks_access_connector_id
  }

  isolation_mode = "ISOLATION_MODE_ISOLATED"
  force_update   = true

  comment = "STG Unity Catalog storage credential using the STG Access Connector managed identity."
}

resource "databricks_external_location" "stg" {
  name            = "el-aiplatform-stg"
  url             = "abfss://ucroot@stexamplestgdata.dfs.core.windows.net/"
  credential_name = databricks_storage_credential.stg.name
  isolation_mode  = "ISOLATION_MODE_ISOLATED"

  comment = "STG Unity Catalog external location backed by private platform ADLS."
}

resource "databricks_catalog" "stg" {
  name           = "stg"
  storage_root   = "abfss://ucroot@stexamplestgdata.dfs.core.windows.net/"
  isolation_mode = "ISOLATED"

  comment = "Terraform-managed STG catalog backed by platform ADLS."

  depends_on = [
    databricks_external_location.stg
  ]
}

resource "databricks_schema" "platform_test" {
  catalog_name = databricks_catalog.stg.name
  name         = "platform_test"

  comment = "STG platform integration validation schema."
}

# --- Phase 11.5 STG Unity Catalog workspace bindings -----------------------

resource "databricks_workspace_binding" "stg_catalog" {
  securable_name = databricks_catalog.stg.name
  securable_type = "catalog"
  workspace_id   = tonumber(module.databricks_workspace.databricks_workspace_numeric_id)
  binding_type   = "BINDING_TYPE_READ_WRITE"
}

resource "databricks_workspace_binding" "stg_external_location" {
  securable_name = databricks_external_location.stg.name
  securable_type = "external_location"
  workspace_id   = tonumber(module.databricks_workspace.databricks_workspace_numeric_id)
  binding_type   = "BINDING_TYPE_READ_WRITE"
}

resource "databricks_workspace_binding" "stg_storage_credential" {
  securable_name = databricks_storage_credential.stg.name
  securable_type = "storage_credential"
  workspace_id   = tonumber(module.databricks_workspace.databricks_workspace_numeric_id)
  binding_type   = "BINDING_TYPE_READ_WRITE"
}

# --- Phase 14.11 hello-databricks product Unity Catalog ---------------------
#
# First consumer of the reusable product-UC primitive: stg.hello_databricks,
# a product-owned schema inside the platform-owned STG catalog. The catalog,
# its storage root and its bindings are untouched.
#
# The runtime principal is READ FROM THE PRODUCT BUNDLE rather than restated
# here. products/hello-databricks/databricks.yml is the authoritative location
# for each target's run-as identity, and copying the client ID into Terraform
# would create a second registry of the same value that can drift silently.
locals {
  hello_databricks_bundle = yamldecode(
    file("${path.module}/../../../products/hello-databricks/databricks.yml")
  )

  hello_databricks_runtime_principal = (
    local.hello_databricks_bundle.targets.stg.run_as.service_principal_name
  )
}

module "hello_databricks_uc" {
  source = "../../modules/databricks_product_uc"

  catalog_name      = databricks_catalog.stg.name
  schema_name       = "hello_databricks"
  runtime_principal = local.hello_databricks_runtime_principal

  comment = "hello-databricks product schema (STG). Managed by Terraform; tables inside are product-owned."
}

# --- Phase 15 ml-lifecycle-demo product Unity Catalog -----------------------
#
# Second consumer of the reusable product-UC primitive: stg.ml_lifecycle_demo,
# a product-owned schema inside the platform-owned STG catalog. The catalog,
# its storage root and its bindings are untouched, and the hello-databricks
# schema above is unaffected — the module grants with databricks_grant
# (singular), so each product's privileges are managed independently.
#
# enable_create_model is true here and only here: this product registers a model
# in the Unity Catalog Model Registry, so its runtime principal needs
# CREATE_MODEL on this one schema. Nothing broader is granted — no
# ALL_PRIVILEGES, no CREATE_SCHEMA, no ownership.
#
# The runtime principal is READ FROM THE PRODUCT BUNDLE for the same reason as
# hello-databricks: products/ml-lifecycle-demo/databricks.yml is the
# authoritative location for each target's run-as identity, and restating the
# client ID here would create a second registry of the same value.
locals {
  ml_lifecycle_demo_bundle = yamldecode(
    file("${path.module}/../../../products/ml-lifecycle-demo/databricks.yml")
  )

  ml_lifecycle_demo_runtime_principal = (
    local.ml_lifecycle_demo_bundle.targets.stg.run_as.service_principal_name
  )
}

module "ml_lifecycle_demo_uc" {
  source = "../../modules/databricks_product_uc"

  catalog_name      = databricks_catalog.stg.name
  schema_name       = "ml_lifecycle_demo"
  runtime_principal = local.ml_lifecycle_demo_runtime_principal

  enable_create_model = true

  comment = "ml-lifecycle-demo product schema (STG). Managed by Terraform; tables and registered models inside are product-owned."
}
