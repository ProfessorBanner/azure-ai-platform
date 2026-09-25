# Key Vault for dev platform workload secrets.
#
# Governance posture:
#   - Azure RBAC authorization (no access policies). Consumer role grants are
#     deferred to the stage that introduces the identity needing them.
#   - Purge protection enabled and soft delete retained, so a deleted vault (or
#     secret) is recoverable within the retention window and cannot be
#     permanently purged early.
#   - No secrets are created here.
#
# TEMPORARY dev network posture: public network access remains enabled for this
# lab so it can be reached from Microsoft-hosted CI agents and the operator's
# workstation without a private endpoint. This is a deliberate, documented
# deviation to revisit when platform networking is designed (see ADR 0004); it
# is NOT the intended production posture.
resource "azurerm_key_vault" "this" {
  name                = var.key_vault_name
  resource_group_name = var.resource_group_name
  location            = var.location
  tenant_id           = var.tenant_id

  sku_name = "standard"

  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = var.soft_delete_retention_days

  public_network_access_enabled = var.public_network_access_enabled

  # Temporary accepted deviation for this dev lab: the vault is reachable over
  # its public endpoint (default_action = Allow) because CI agents and the
  # operator workstation have no stable egress or private endpoint yet. Trusted
  # Azure services may bypass. Review when platform networking lands (ADR 0004).
  #trivy:ignore:AVD-AZU-0013:exp:2026-11-30
  network_acls {
    default_action = var.public_network_access_enabled ? "Allow" : "Deny"
    bypass         = "AzureServices"
  }

  tags = var.tags

  lifecycle {
    prevent_destroy = true
  }
}
