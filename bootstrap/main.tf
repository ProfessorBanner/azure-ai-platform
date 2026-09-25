resource "azurerm_resource_group" "bootstrap" {
  name     = var.resource_group_name
  location = var.location

  tags = {
    environment = "dev"
    purpose     = "terraform-bootstrap"
    managed_by  = "terraform"
  }
}

# Temporary accepted deviation:
# The Terraform state account remains accessible through its public endpoint
# until pipeline/workstation network access or a private-endpoint design is implemented.
# Review before 2026-11-30.
#trivy:ignore:AZU-0012:exp:2026-11-30
resource "azurerm_storage_account" "terraform_state" {
  name                = var.storage_account_name
  resource_group_name = azurerm_resource_group.bootstrap.name
  location            = azurerm_resource_group.bootstrap.location

  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"

  min_tls_version               = "TLS1_2"
  https_traffic_only_enabled    = true
  public_network_access_enabled = true # Deferred: public access unchanged in this change (see ADR 0002).

  # FOLLOW-UP CHANGE (deliberately deferred; not flipped here).
  # The backend already authenticates with Microsoft Entra ID (use_azuread_auth = true),
  # so disabling Shared Key is safe, but it is gated to its own reviewed apply. To adopt:
  # set this to `false`, apply on its own, then re-run `terraform init -reconfigure` and
  # `terraform plan` to confirm the backend still authenticates. See ADR 0002.
  shared_access_key_enabled       = true
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = 30
    }

    container_delete_retention_policy {
      days = 30
    }
  }

  tags = {
    environment = "dev"
    purpose     = "terraform-state"
    managed_by  = "terraform"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_storage_container" "terraform_state" {
  name                  = var.container_name
  storage_account_id    = azurerm_storage_account.terraform_state.id
  container_access_type = "private"
}
