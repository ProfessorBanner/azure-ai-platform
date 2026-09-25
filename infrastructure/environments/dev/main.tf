# Dev Azure platform foundation (state key: platform/dev.tfstate).
#
# This root composes the dev platform building blocks — ADLS Gen2 storage, Key
# Vault, a Log Analytics workspace, and (Phase 7) a Premium Azure Databricks
# workspace with an Access Connector — into the externally provisioned resource
# group rg-aiplatform-dev. Foundry, networking, Container Apps, Cosmos DB, AI
# Search and application resources remain out of scope (see ADR 0004 and 0005).
#
# DEV NOTE: storage, Key Vault and Log Analytics are ALREADY DEPLOYED under
# platform/dev.tfstate. Their module blocks and Terraform addresses below are
# unchanged; Phase 7 only ADDS the two databricks_* modules. Do not rename,
# re-nest or re-key the existing modules — that would churn their addresses.

locals {
  # Common tag schema applied to every resource; each module also receives a
  # resource-specific `purpose`.
  common_tags = {
    environment = var.environment
    platform    = "azure-ai-platform"
    managed_by  = "terraform"
  }
}

# rg-aiplatform-dev is provisioned OUTSIDE this configuration (externally
# bootstrapped). It is referenced by data source so Terraform can place
# resources into it without ever creating or destroying the group itself.
data "azurerm_resource_group" "platform" {
  name = var.resource_group_name
}

module "storage" {
  source = "../../modules/storage"

  storage_account_name = var.storage_account_name
  resource_group_name  = data.azurerm_resource_group.platform.name
  location             = var.location
  tags                 = merge(local.common_tags, { purpose = "data-lake" })
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
  tags                = merge(local.common_tags, { purpose = "platform-observability" })
}

# --- Phase 7 additions (new Terraform addresses; expected: 2 to add) ---------
# Azure Databricks DEV foundation: the durable workspace and its Access
# Connector (system-assigned managed identity) only. No clusters, jobs,
# notebooks, role assignments or Unity Catalog objects — those are Phase 8
# (see ADR 0005).
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
    "10.20.0.0/24"
  ]

  databricks_host_subnet_prefixes = [
    "10.20.0.0/26"
  ]

  databricks_container_subnet_prefixes = [
    "10.20.0.64/26"
  ]

  private_endpoint_subnet_prefixes = [
    "10.20.0.128/27"
  ]

  tags = merge(local.common_tags, {
    purpose = "platform-network"
  })
}

# --- Phase 9D storage private connectivity ----------------------------------
#
# DEV ADLS private-link path, reusing the module proven in SANDBOX. With
# stexampledevdata at publicNetworkAccess=Disabled + default_action=Deny, the
# VNet-injected DEV Databricks classic compute reaches ADLS only through these
# private endpoints and linked private DNS zones. Same invocation pattern as the
# sandbox root; only the module outputs differ (dev storage/network).
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

# --- Phase 9D.6 Unity Catalog storage RBAC ----------------------------------
#
# Grant the DEV Databricks Access Connector's system-assigned managed identity
# Storage Blob Data Contributor over the DEV ADLS Gen2 account. This is the
# single Azure-layer prerequisite before any Databricks/Unity Catalog object
# (storage credential, external location, catalog managed location) can bind
# stexampledevdata. Codified in Terraform for the governed DEV environment,
# unlike the historical manual SANDBOX grant.
resource "azurerm_role_assignment" "databricks_access_connector_storage" {
  scope                = module.storage.storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.databricks_access_connector.databricks_access_connector_principal_id
}

# --- Phase 9D.7 Unity Catalog root filesystem -------------------------------
#
# The DEV Unity Catalog managed-storage root, modelled as an ADLS Gen2
# filesystem (a blob container on the hierarchical-namespace account). Created
# via the ARM management plane through AzAPI rather than the AzureRM
# data-plane resources (azurerm_storage_container /
# azurerm_storage_data_lake_gen2_filesystem), so the Terraform runner never
# needs Storage data-plane network access — the account stays
# publicNetworkAccess=Disabled + default_action=Deny with no firewall
# exception. publicAccess=None keeps the container private (no anonymous
# blob/container access).
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

# --- Phase 9D.8 Unity Catalog storage credential ----------------------------
#
# DEV Unity Catalog storage credential backed by the existing Azure Databricks
# Access Connector's system-assigned managed identity (ac-aiplatform-databricks-
# dev). No secret, key, SAS or service principal — authentication is entirely
# via the managed identity, whose Storage Blob Data Contributor grant on
# stexampledevdata already exists (Gate 9D.6). The external location, catalog
# and schema are out of scope for this gate.
resource "databricks_storage_credential" "dev" {
  name = "sc-aiplatform-dev"

  azure_managed_identity {
    access_connector_id = module.databricks_access_connector.databricks_access_connector_id
  }

  isolation_mode = "ISOLATION_MODE_ISOLATED"
  force_update   = true

  comment = "DEV Unity Catalog storage credential using the ac-aiplatform-databricks-dev Access Connector managed identity."
}

# --- Phase 9D.9 Unity Catalog external location -----------------------------
#
# DEV Unity Catalog external location over the private platform ADLS ucroot
# filesystem, backed by the managed-identity storage credential above. The
# abfss:// (DFS) path is required — not blob/https, not a DBFS mount. Left
# writable (no read_only) to support later DEV managed-storage validation.
# skip_validation is intentionally omitted so Databricks validates the path and
# credential (over the private endpoints) at create time; no file events.
resource "databricks_external_location" "dev" {
  name            = "el-aiplatform-dev"
  url             = "abfss://ucroot@stexampledevdata.dfs.core.windows.net/"
  credential_name = databricks_storage_credential.dev.name
  isolation_mode  = "ISOLATION_MODE_ISOLATED"

  comment = "DEV Unity Catalog external location backed by private platform ADLS."
}
# --- Phase 9D.10a Unity Catalog DEV catalog ---------------------------------
#
# Explicit Terraform-managed DEV catalog backed by the platform-managed ADLS
# root exposed through el-aiplatform-dev. The Databricks-generated
# dbw_aiplatform_dev catalog remains untouched.
#
# Catalog-level managed storage provides the canonical DEV namespace boundary.
# ISOLATED mode restricts the catalog to explicitly bound workspaces; explicit
# binding management is reviewed separately in Gate 9D.11.
resource "databricks_catalog" "dev" {
  name           = "dev"
  storage_root   = "abfss://ucroot@stexampledevdata.dfs.core.windows.net/"
  isolation_mode = "ISOLATED"

  comment = "Terraform-managed DEV catalog backed by platform ADLS."
}

# --- Phase 9D.10b DEV validation schema -------------------------------------
#
# Minimal schema used only to prove the complete DEV Unity Catalog managed-
# storage path before product-specific schemas are introduced. No independent
# storage root is set: managed objects inherit the DEV catalog-level storage.
resource "databricks_schema" "platform_test" {
  catalog_name = databricks_catalog.dev.name
  name         = "platform_test"

  comment = "DEV platform integration validation schema."
}

# --- Phase 9D.11 Unity Catalog workspace isolation bindings -----------------
#
# Explicitly codify the DEV-only workspace bindings for the isolated Unity
# Catalog securables. These bindings already exist in the live metastore and
# have been adopted into Terraform state.
resource "databricks_workspace_binding" "dev_catalog" {
  securable_name = databricks_catalog.dev.name
  securable_type = "catalog"
  workspace_id   = tonumber(module.databricks_workspace.databricks_workspace_numeric_id)
  binding_type   = "BINDING_TYPE_READ_WRITE"
}

resource "databricks_workspace_binding" "dev_external_location" {
  securable_name = databricks_external_location.dev.name
  securable_type = "external_location"
  workspace_id   = tonumber(module.databricks_workspace.databricks_workspace_numeric_id)
  binding_type   = "BINDING_TYPE_READ_WRITE"
}

resource "databricks_workspace_binding" "dev_storage_credential" {
  securable_name = databricks_storage_credential.dev.name
  securable_type = "storage_credential"
  workspace_id   = tonumber(module.databricks_workspace.databricks_workspace_numeric_id)
  binding_type   = "BINDING_TYPE_READ_WRITE"
}

# --- Phase 14.11 hello-databricks product Unity Catalog ---------------------
#
# First consumer of the reusable product-UC primitive: dev.hello_databricks,
# a product-owned schema inside the platform-owned DEV catalog. The catalog,
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
    local.hello_databricks_bundle.targets.dev.run_as.service_principal_name
  )
}

module "hello_databricks_uc" {
  source = "../../modules/databricks_product_uc"

  catalog_name      = databricks_catalog.dev.name
  schema_name       = "hello_databricks"
  runtime_principal = local.hello_databricks_runtime_principal

  comment = "hello-databricks product schema (DEV). Managed by Terraform; tables inside are product-owned."
}

# --- Phase 15 ml-lifecycle-demo product Unity Catalog -----------------------
#
# Second consumer of the reusable product-UC primitive: dev.ml_lifecycle_demo,
# a product-owned schema inside the platform-owned DEV catalog. The catalog,
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
    local.ml_lifecycle_demo_bundle.targets.dev.run_as.service_principal_name
  )
}

module "ml_lifecycle_demo_uc" {
  source = "../../modules/databricks_product_uc"

  catalog_name      = databricks_catalog.dev.name
  schema_name       = "ml_lifecycle_demo"
  runtime_principal = local.ml_lifecycle_demo_runtime_principal

  enable_create_model = true

  comment = "ml-lifecycle-demo product schema (DEV). Managed by Terraform; tables and registered models inside are product-owned."
}
