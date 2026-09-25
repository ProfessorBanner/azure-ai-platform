# Retained artifact storage — lifecycle=retained, OUTSIDE the session deletion
# scope. Holds the immutable training inputs the session stages before compute
# exists, the model packages a session exports before teardown, and the
# watchdog's durable job results.
#
# Cost (UK South retail, 2026-09-17 evidence): Hot LRS data USD 0.0192 per
# GB-month plus operation charges; a few GB costs cents per month. The
# lifecycle rule below is the explicit cleanup policy: blobs are deleted
# retained_blob_expiry_days after last modification, so nothing accumulates
# past the project's horizon without a deliberate re-upload.
#
# NO KEYS: shared-key access is disabled and every data-plane call uses Entra
# ID (operator RBAC, the AKS kubelet identity, the Automation managed identity).
# The public endpoint stays enabled but default-deny: only allowed_ip_cidrs
# (operator egress, and in G4 the session cluster's outbound IP) can reach it.

locals {
  retained_storage_account_name = coalesce(
    var.retained_storage_account_name,
    "staksmlops${substr(sha256(var.subscription_id), 0, 8)}"
  )
}

resource "azurerm_storage_account" "retained" {
  name                            = local.retained_storage_account_name
  resource_group_name             = azurerm_resource_group.retained.name
  location                        = azurerm_resource_group.retained.location
  account_kind                    = "StorageV2"
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  access_tier                     = "Hot"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  public_network_access_enabled   = true

  blob_properties {
    versioning_enabled = false
  }

  # Default-deny network rules: only the runtime-supplied operator/cluster
  # addresses reach the data plane. Trusted Azure services bypass covers
  # platform logging; Automation is NOT a trusted service (documented).
  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
    ip_rules       = var.allowed_ip_cidrs
  }

  tags = local.retained_tags
}

resource "azurerm_storage_container" "inputs" {
  name                  = "inputs"
  storage_account_id    = azurerm_storage_account.retained.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "models" {
  name                  = "models"
  storage_account_id    = azurerm_storage_account.retained.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "results" {
  name                  = "watchdog-results"
  storage_account_id    = azurerm_storage_account.retained.id
  container_access_type = "private"
}

resource "azurerm_storage_management_policy" "retained_expiry" {
  storage_account_id = azurerm_storage_account.retained.id

  rule {
    name    = "expire-retained-blobs"
    enabled = true

    filters {
      blob_types = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = var.retained_blob_expiry_days
      }
    }
  }
}

# The watchdog writes one JSON result per job to watchdog-results. Container
# scope only: it cannot read inputs or models.
resource "azurerm_role_assignment" "watchdog_results_writer" {
  scope                = azurerm_storage_container.results.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_automation_account.watchdog.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}
