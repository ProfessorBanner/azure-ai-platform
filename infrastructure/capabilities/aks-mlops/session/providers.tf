provider "azurerm" {
  features {}

  subscription_id = var.subscription_id

  # Never register resource providers as a side effect of a plan.
  resource_provider_registrations = "none"
}
