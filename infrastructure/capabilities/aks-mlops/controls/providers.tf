provider "azurerm" {
  features {
    resource_group {
      # The session resource group is emptied by the watchdog, never by this
      # root; an accidental destroy must not delete resources it does not own.
      prevent_deletion_if_contains_resources = true
    }
  }

  subscription_id = var.subscription_id

  # Read-only plans must never register resource providers as a side effect.
  # azurerm 4.x replaced skip_provider_registration with this setting; "none"
  # means the provider registers nothing and fails clearly if a provider the
  # configuration needs is unregistered (discovery records the state up front).
  resource_provider_registrations = "none"
}
