# Optional Azure budget: a SUPPLEMENTARY signal, not a control. Azure budgets
# evaluate on a monthly period and reset; the lifetime project ledger in
# products/pet-classifier/scripts/g3 does not. A budget cannot stop spend and
# its evaluation lags billing by hours, so it never substitutes for the ledger,
# the reservation or the watchdog.
#
# Subscription scope with a ResourceGroupName filter so the AKS-managed node
# group (which does not exist until compute is provisioned) is covered from the
# first hour it appears; a resource-group-scoped budget cannot be created for a
# group that does not exist yet.
resource "azurerm_consumption_budget_subscription" "aksmlops" {
  count = var.budget_alert.enabled ? 1 : 0

  name            = "budget-aiplatform-aksmlops-monthly"
  subscription_id = "/subscriptions/${var.subscription_id}"
  amount          = var.budget_policy.admission_limit_usd
  time_grain      = "Monthly"

  time_period {
    start_date = var.budget_alert.start_date
    end_date   = var.budget_alert.end_date
  }

  filter {
    dimension {
      name     = "ResourceGroupName"
      operator = "In"
      values = [
        var.session_resource_group_name,
        var.session_node_resource_group_name,
        azurerm_resource_group.controls.name,
        azurerm_resource_group.retained.name,
      ]
    }
  }

  notification {
    enabled        = true
    threshold      = 25
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = var.budget_alert.contact_emails
  }

  notification {
    enabled        = true
    threshold      = 50
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = var.budget_alert.contact_emails
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Forecasted"
    contact_emails = var.budget_alert.contact_emails
  }
}
