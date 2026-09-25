# Microsoft Foundry capability lab — SANDBOX
# (state key: capabilities/ai-foundry/sandbox.tfstate).
#
# A CAPABILITY root, not an environment root. It lives outside
# infrastructure/environments/ because Foundry is a bounded, disposable
# exploration rather than a tier of the governed dev -> stg -> prod platform, and
# because a metered inference deployment must be creatable and destroyable
# without any risk to the four platform environment states.
#
# It ADDS to the existing sandbox resource group. Everything it depends on —
# the resource group and the Log Analytics workspace — is read through data
# sources and is owned by platform/sandbox.tfstate. This root creates no
# resource group, no storage, no Key Vault, no networking and no role
# assignment. See docs/adr/0006-foundry-capability-lab.md.

locals {
  common_tags = {
    environment = var.environment
    platform    = "azure-ai-platform"
    managed_by  = "terraform"
    lifecycle   = "disposable"
    capability  = "ai-foundry"
  }
}

# rg-aiplatform-sandbox is provisioned OUTSIDE this configuration and managed by
# the platform sandbox root. Referenced by data source so this configuration can
# never create or destroy it.
data "azurerm_resource_group" "sandbox" {
  name = var.resource_group_name
}

# The sandbox Log Analytics workspace is owned by platform/sandbox.tfstate. Read
# by data source rather than by terraform_remote_state, so this capability keeps
# no coupling to platform state and stays independently destroyable.
data "azurerm_log_analytics_workspace" "sandbox" {
  name                = var.log_analytics_workspace_name
  resource_group_name = data.azurerm_resource_group.sandbox.name
}

module "ai_foundry" {
  source = "../../../modules/ai-foundry"

  account_name          = var.foundry_account_name
  custom_subdomain_name = var.foundry_custom_subdomain_name
  resource_group_name   = data.azurerm_resource_group.sandbox.name
  location              = var.location

  public_network_access_enabled = true
  network_acls_default_action   = "Deny"
  allowed_ip_cidrs              = var.allowed_ip_cidrs

  project_name         = var.foundry_project_name
  project_display_name = var.foundry_project_display_name
  project_description  = var.foundry_project_description

  deployment_name        = var.deployment_name
  model_name             = var.model_name
  model_version          = var.model_version
  deployment_sku_name    = var.deployment_sku_name
  deployment_capacity    = var.deployment_capacity
  version_upgrade_option = var.version_upgrade_option

  log_analytics_workspace_id = data.azurerm_log_analytics_workspace.sandbox.id

  tags = merge(local.common_tags, { purpose = "foundry-capability-lab" })
}

# --- Container registry for the Phase 19.1e Hosted Agent ---------------------
#
# A Hosted Agent runs an image that Foundry pulls, so the capability needs a
# registry. It lives in this capability root rather than the platform roots for
# the reason ADR 0006 gives about the Foundry account itself: it is disposable,
# it exists only in sandbox, and it must be destroyable without a plan ever
# touching platform/{sandbox,dev,stg,prod}.tfstate.
#
# NO CREDENTIALS ARE CREATED OR STORED. The admin account stays disabled, so the
# registry issues no username/password pair for anything to leak; pushes use the
# operator's Entra identity via `az acr login`, and pulls use the Foundry
# project's managed identity through the role assignment below.

locals {
  # Deterministic and globally unique by construction. ACR names share one
  # global namespace, so a bare "acraiplatformsandbox" would collide with any
  # other tenant that thought of it first. The subscription hash makes the name
  # stable for a given subscription and effectively unique across tenants,
  # without needing a random_id that would churn state.
  container_registry_name = (
    var.container_registry_name != null
    ? var.container_registry_name
    : substr(lower("acraiplatform${var.environment}${substr(sha1(var.subscription_id), 0, 10)}"), 0, 50)
  )

  # The pull role is DERIVED from the permission model rather than configured
  # beside it. Under ABAC repository permissions the registry-wide AcrPull is
  # not the role to use; under the legacy model the repository roles are not
  # honoured. Deriving it means the two can never be set inconsistently.
  container_registry_pull_role = (
    var.container_registry_role_assignment_mode == "AbacRepositoryPermissions"
    ? "Container Registry Repository Reader"
    : "AcrPull"
  )
}

resource "azurerm_container_registry" "hosted_agent" {
  name                = local.container_registry_name
  resource_group_name = data.azurerm_resource_group.sandbox.name
  location            = var.location

  # Basic is sufficient for one small image in a sandbox proof and is the
  # cheapest SKU that exists. It does NOT support private endpoints; moving to
  # private networking later is a deliberate SKU change, not a flag.
  sku = "Basic"

  # No admin account: it is a shared username/password pair, which is exactly
  # the kind of long-lived credential this platform refuses to create.
  admin_enabled = false

  public_network_access_enabled = var.container_registry_public_access_enabled
  role_assignment_mode          = var.container_registry_role_assignment_mode

  tags = merge(local.common_tags, { purpose = "phase19-hosted-agent" })
}

# Lets the Foundry PROJECT identity pull the Hosted Agent image. Scoped to this
# registry and to a read-only role: nothing here can push, delete or administer.
resource "azurerm_role_assignment" "foundry_project_pulls_hosted_agent" {
  scope                = azurerm_container_registry.hosted_agent.id
  role_definition_name = local.container_registry_pull_role
  principal_id         = module.ai_foundry.project_principal_id

  description = "Allows the Foundry project managed identity to pull the Phase 19.1e Hosted Agent image."
}
