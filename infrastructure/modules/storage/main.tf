# ADLS Gen2 storage account for the dev platform.
#
# Intended as the durable Data Lake / future Databricks storage account. It is
# created empty: no bronze/silver/gold containers are provisioned in this stage.
#
# Security posture:
#   - StorageV2 with hierarchical namespace (ADLS Gen2).
#   - TLS 1.2 minimum, HTTPS-only.
#   - No public blob/container access (allow_nested_items_to_be_public = false).
#   - Shared Key authentication disabled: this account is only ever reached with
#     Microsoft Entra ID. No data-plane objects are created by Terraform, so no
#     Shared Key dependency exists.
#   - Public network access is default-denied; the data plane is unreachable
#     until networking (private endpoint / selected rules) is designed in a
#     later stage.
#
# Note on versioning: Azure blob versioning is NOT supported on accounts with a
# hierarchical namespace (ADLS Gen2). Data protection is therefore provided by
# blob and container soft delete rather than versioning.
resource "azurerm_storage_account" "this" {
  name                = var.storage_account_name
  resource_group_name = var.resource_group_name
  location            = var.location

  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  is_hns_enabled           = true

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  public_network_access_enabled   = var.public_network_access_enabled
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false

  # Default-deny data-plane network access. No IP or subnet rules are added in
  # this stage (networking is deferred); trusted Azure services may bypass.
  # Complementary to public_network_access_enabled = false.
  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
  }

  blob_properties {
    # Unsupported with hierarchical namespace; soft delete is the protection.
    versioning_enabled = false

    delete_retention_policy {
      days = var.blob_soft_delete_retention_days
    }

    container_delete_retention_policy {
      days = var.container_soft_delete_retention_days
    }
  }

  tags = var.tags

  lifecycle {
    prevent_destroy = true
  }
}
