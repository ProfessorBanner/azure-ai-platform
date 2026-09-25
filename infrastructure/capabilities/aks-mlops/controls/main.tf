# AKS MLOps cost controls — CONTROLS root
# (state key: capabilities/aks-mlops/controls.tfstate).
#
# A CAPABILITY root, like the Foundry lab (ADR 0006): a bounded, disposable
# exploration that must never touch platform/{sandbox,dev,stg,prod}.tfstate.
# Unlike the Foundry lab it does NOT add to an existing platform resource
# group: it owns three small groups of its own so that nothing here reuses or
# is granted access to existing platform resources.
#
#   rg-aiplatform-aksmlops-controls   lifecycle=control    watchdog + identity
#   rg-aiplatform-aksmlops-retained   lifecycle=retained   inputs, models, results
#   rg-aiplatform-aksmlops-session    lifecycle=disposable EMPTY shell; the
#                                     session root deploys AKS into it and the
#                                     watchdog is allowed to empty it.
#
# Creating the session group here, empty, is deliberate: the watchdog's role
# assignment is scoped to this group and must exist BEFORE compute does.
# See docs/adr/0012-aks-mlops-cost-controls.md.

locals {
  common_tags = {
    project     = var.project
    environment = "sandbox"
    owner       = var.owner
    platform    = "azure-ai-platform"
    managed_by  = "terraform"
    capability  = "aks-mlops"
    expiry_utc  = var.controls_expiry_utc
  }

  control_tags   = merge(local.common_tags, { lifecycle = "control" })
  retained_tags  = merge(local.common_tags, { lifecycle = "retained" })
  armed          = var.armed_session != null
  armed_id       = local.armed ? var.armed_session.session_id : "none"
  armed_expiry   = local.armed ? var.armed_session.expiry_utc : "none"
  session_rg_id  = "/subscriptions/${var.subscription_id}/resourceGroups/${var.session_resource_group_name}"
  node_rg_id     = "/subscriptions/${var.subscription_id}/resourceGroups/${var.session_node_resource_group_name}"
  automation_ver = "7.4"
}

# --- Resource groups -----------------------------------------------------------

resource "azurerm_resource_group" "controls" {
  name     = "rg-aiplatform-aksmlops-controls"
  location = var.location
  tags     = local.control_tags
}

resource "azurerm_resource_group" "retained" {
  name     = var.retained_resource_group_name
  location = var.location
  tags     = local.retained_tags
}

# The disposable session group. Its tags are the watchdog's second key: the
# runbook refuses to touch a group whose project / session_id / expiry_utc /
# lifecycle tags do not match what this root armed.
resource "azurerm_resource_group" "session" {
  name     = var.session_resource_group_name
  location = var.location
  tags = merge(local.common_tags, {
    lifecycle  = "disposable"
    session_id = local.armed_id
    expiry_utc = local.armed_expiry
  })
}

# --- Watchdog identity: a custom role, scoped to the session group only ---------
#
# Not Contributor. The role can read, delete the resource types the session root
# creates, and nothing else. assignable_scopes pins it to the session group so
# it cannot be re-used more widely by accident.
resource "azurerm_role_definition" "session_janitor" {
  name        = "aksmlops-session-janitor"
  scope       = azurerm_resource_group.session.id
  description = "Delete-only role for the AKS MLOps expiry watchdog: read inventory and delete the disposable session resources. Scoped to the session resource group."

  permissions {
    actions = [
      "*/read",
      "Microsoft.ContainerService/managedClusters/delete",
      "Microsoft.ContainerRegistry/registries/delete",
      "Microsoft.ManagedIdentity/userAssignedIdentities/delete",
      "Microsoft.Resources/deployments/delete",
    ]
    not_actions = []
  }

  assignable_scopes = [azurerm_resource_group.session.id]
}

resource "azurerm_role_assignment" "watchdog_session_janitor" {
  scope              = azurerm_resource_group.session.id
  role_definition_id = azurerm_role_definition.session_janitor.role_definition_resource_id
  principal_id       = azurerm_automation_account.watchdog.identity[0].principal_id
  principal_type     = "ServicePrincipal"
}

# Subscription-wide READ ONLY. Justification: after deleting the cluster the
# watchdog must re-inventory the AKS-managed node resource group, which AKS
# creates and deletes itself and which does not exist when this root is
# applied, so it cannot be a role-assignment scope. Reader grants no write or
# delete anywhere; every mutation the identity can perform is confined to the
# session group by the custom role above.
resource "azurerm_role_assignment" "watchdog_subscription_reader" {
  scope                = "/subscriptions/${var.subscription_id}"
  role_definition_name = "Reader"
  principal_id         = azurerm_automation_account.watchdog.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# --- Azure Automation: the expiry watchdog ---------------------------------------
#
# Chosen because it survives a closed laptop, a disconnected CLI, a broken AKS
# control plane and missing pods: the schedule and the job run in Azure, under
# the account's managed identity, with no credential stored anywhere.
# Verified against current documentation (2026-09-17):
#   - schedules run once or recur; the most frequent recurrence is one hour;
#   - PowerShell 7.4 runtime environments are supported for cloud jobs in all
#     regions, with Az 12.3.0 installed by default;
#   - fair share stops any job after three hours; the runbook budgets well under;
#   - job history is kept for 30 days; results are also written to blob storage.
resource "azurerm_automation_account" "watchdog" {
  name                          = "aa-aiplatform-aksmlops-watchdog"
  location                      = azurerm_resource_group.controls.location
  resource_group_name           = azurerm_resource_group.controls.name
  sku_name                      = "Basic"
  local_authentication_enabled  = false
  public_network_access_enabled = var.automation_public_network_access_enabled

  identity {
    type = "SystemAssigned"
  }

  tags = local.control_tags
}

resource "azurerm_automation_runtime_environment" "powershell74" {
  name                  = "aksmlops-ps74"
  automation_account_id = azurerm_automation_account.watchdog.id
  location              = azurerm_resource_group.controls.location
  description           = "PowerShell 7.4 with the default Az modules for the expiry watchdog."
  runtime_language      = "PowerShell"
  runtime_version       = local.automation_ver

  runtime_default_packages = {
    "Az" = "12.3.0"
  }
}

# Allowlist the runbook reads with Get-AutomationVariable. Everything the
# runbook is permitted to delete is pinned by these values, not by parameters
# alone: a schedule parameter must AGREE with them or the job refuses.
resource "azurerm_automation_variable_string" "allowlist" {
  for_each = {
    "aksmlops-subscription-id"         = var.subscription_id
    "aksmlops-project"                 = var.project
    "aksmlops-session-rg-id"           = local.session_rg_id
    "aksmlops-session-node-rg-id"      = local.node_rg_id
    "aksmlops-armed-session-id"        = local.armed_id
    "aksmlops-armed-expiry-utc"        = local.armed_expiry
    "aksmlops-results-storage-account" = azurerm_storage_account.retained.name
    "aksmlops-results-container"       = azurerm_storage_container.results.name
  }

  name                    = each.key
  resource_group_name     = azurerm_resource_group.controls.name
  automation_account_name = azurerm_automation_account.watchdog.name
  value                   = each.value
  encrypted               = false
  description             = "Watchdog allowlist value managed by Terraform (controls root)."
}

resource "azurerm_automation_runbook" "session_expiry" {
  name                     = "Invoke-SessionExpiry"
  location                 = azurerm_resource_group.controls.location
  resource_group_name      = azurerm_resource_group.controls.name
  automation_account_name  = azurerm_automation_account.watchdog.name
  runbook_type             = "PowerShell"
  runtime_environment_name = azurerm_automation_runtime_environment.powershell74.name
  log_verbose              = true
  log_progress             = false
  description              = "Expiry watchdog: report (default) or delete disposable session resources whose armed expiry has passed. Idempotent; polls deletion; re-inventories; never touches control or retained resources."
  content                  = file("${path.module}/runbooks/Invoke-SessionExpiry.ps1")

  tags = local.control_tags
}

# Independent recurring sweep: every hour (the most frequent supported cadence)
# regardless of whether a one-time schedule fired. Catches a missed or failed
# one-time job and reports residual resources after a partial deletion.
resource "azurerm_automation_schedule" "sweep_hourly" {
  name                    = "aksmlops-sweep-hourly"
  resource_group_name     = azurerm_resource_group.controls.name
  automation_account_name = azurerm_automation_account.watchdog.name
  frequency               = "Hour"
  interval                = 1
  timezone                = "Etc/UTC"
  start_time              = var.sweep_start_time_utc
  description             = "Hourly report/cleanup sweep of the armed session. Independent of the one-time expiry schedule."
}

resource "azurerm_automation_job_schedule" "sweep_hourly" {
  resource_group_name     = azurerm_resource_group.controls.name
  automation_account_name = azurerm_automation_account.watchdog.name
  schedule_name           = azurerm_automation_schedule.sweep_hourly.name
  runbook_name            = azurerm_automation_runbook.session_expiry.name

  parameters = {
    mode    = var.watchdog_mode
    trigger = "sweep"
  }
}

# One-time schedule at the armed session's expiry. Created only when a session
# is armed; its start time IS the expiry, so arming and the deadline are the
# same Terraform change and the deadline is defined from the reservation.
resource "azurerm_automation_schedule" "session_expiry" {
  count = local.armed ? 1 : 0

  name                    = "aksmlops-expiry-${var.armed_session.session_id}"
  resource_group_name     = azurerm_resource_group.controls.name
  automation_account_name = azurerm_automation_account.watchdog.name
  frequency               = "OneTime"
  timezone                = "Etc/UTC"
  start_time              = var.armed_session.expiry_utc
  description             = "One-time expiry for session ${var.armed_session.session_id}."
}

resource "azurerm_automation_job_schedule" "session_expiry" {
  count = local.armed ? 1 : 0

  resource_group_name     = azurerm_resource_group.controls.name
  automation_account_name = azurerm_automation_account.watchdog.name
  schedule_name           = azurerm_automation_schedule.session_expiry[0].name
  runbook_name            = azurerm_automation_runbook.session_expiry.name

  parameters = {
    mode      = var.watchdog_mode
    trigger   = "expiry"
    sessionid = var.armed_session.session_id
  }
}
