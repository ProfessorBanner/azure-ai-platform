# Microsoft Foundry capability lab: one AIServices account, one project and one
# model deployment.
#
# This is the MODERN Foundry resource model — an `azurerm_cognitive_account` of
# kind "AIServices" with `project_management_enabled = true`, carrying
# `azurerm_cognitive_account_project` children. It is deliberately NOT the legacy
# AI Hub (`azurerm_ai_foundry`), which is an Azure Machine Learning workspace and
# would force a Key Vault, a storage account and a container registry into scope.
#
# Security posture:
#   - system-assigned managed identity on both the account and the project;
#   - `local_auth_enabled = false`, so API keys are disabled and every caller
#     must present a Microsoft Entra ID token (there is no secret to leak);
#   - a custom subdomain, which Entra token authentication requires and which
#     `network_acls` also requires;
#   - `network_acls.default_action = "Deny"`, so the public endpoint is reachable
#     only from explicitly supplied IPv4 CIDRs.
#
# Cost posture: the S0 account itself carries no standing charge; spend is driven
# by the deployment's token consumption, bounded by its provisioned capacity.

locals {
  # Cognitive Services rejects host-sized prefixes (/31 and /32) in ip_rules, so
  # a single address is submitted bare. Wider prefixes are passed through as CIDR.
  normalised_ip_rules = [
    for cidr in var.allowed_ip_cidrs :
    can(regex("/3[12]$", cidr)) ? split("/", cidr)[0] : cidr
  ]
}

resource "azurerm_cognitive_account" "this" {
  name                = var.account_name
  resource_group_name = var.resource_group_name
  location            = var.location

  kind     = "AIServices"
  sku_name = var.account_sku_name

  # Globally unique; required for Entra ID authentication and for network_acls.
  custom_subdomain_name = var.custom_subdomain_name

  # Enables the Foundry project model on this account.
  project_management_enabled = true

  # Keyless only. Phase 17 consumes this account without any secret material.
  local_auth_enabled = false

  # Phase 16 baseline: a public endpoint fenced by a default-deny IP allow-list.
  # Private endpoints and privatelink DNS are explicitly out of scope (ADR 0006).
  public_network_access_enabled = var.public_network_access_enabled

  network_acls {
    default_action = var.network_acls_default_action
    ip_rules       = local.normalised_ip_rules
  }

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}

resource "azurerm_cognitive_account_project" "this" {
  name                 = var.project_name
  cognitive_account_id = azurerm_cognitive_account.this.id
  location             = var.location

  display_name = var.project_display_name
  description  = var.project_description

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}

# The deployment is the metered resource. `version_upgrade_option` is pinned so
# Azure never silently moves the served model version underneath a consumer —
# reproducibility matters more here than staying current automatically.
resource "azurerm_cognitive_deployment" "this" {
  name                 = var.deployment_name
  cognitive_account_id = azurerm_cognitive_account.this.id

  # Serialise creation behind the project.
  #
  # Both this deployment and the project reference only the ACCOUNT, so Terraform
  # infers no ordering between them and creates them concurrently. Azure treats
  # the account as a single mutable resource and rejects the second concurrent
  # child write:
  #
  #   409 RequestConflict: Another operation is in progress on the resource
  #
  # which broke the first clean apply. This is the smallest dependency that
  # enforces account -> project -> deployment. It is a meta-argument only: it
  # changes graph ordering, not resource configuration, so it produces no diff
  # against already-deployed infrastructure.
  depends_on = [azurerm_cognitive_account_project.this]

  version_upgrade_option = var.version_upgrade_option

  model {
    format  = var.model_format
    name    = var.model_name
    version = var.model_version
  }

  sku {
    name     = var.deployment_sku_name
    capacity = var.deployment_capacity
  }
}

# Control-plane and request telemetry into the EXISTING sandbox Log Analytics
# workspace (referenced by the caller, never created here). The workspace carries
# a daily ingestion cap, which bounds the cost of this setting.
resource "azurerm_monitor_diagnostic_setting" "this" {
  name                       = var.diagnostic_setting_name
  target_resource_id         = azurerm_cognitive_account.this.id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category_group = "allLogs"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}
