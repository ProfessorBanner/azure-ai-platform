# Log Analytics workspace for the dev platform.
#
# Cost-conscious posture:
#   - PerGB2018 (pay-as-you-go) SKU: no reserved capacity commitment.
#   - Short retention sized for a personal dev platform.
#   - A small daily ingestion cap bounds spend.
#
# No diagnostic settings are attached in this stage; nothing streams to the
# workspace yet, so ingestion cost is effectively zero until a later stage
# wires up sources.
resource "azurerm_log_analytics_workspace" "this" {
  name                = var.workspace_name
  resource_group_name = var.resource_group_name
  location            = var.location

  sku               = "PerGB2018"
  retention_in_days = var.retention_in_days
  daily_quota_gb    = var.daily_quota_gb

  tags = var.tags
}
