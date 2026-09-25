terraform {
  backend "azurerm" {
    # Independent state: capabilities/aks-mlops/controls.tfstate
    # (backend/capability-aks-mlops-controls.hcl).
    #
    # The controls root owns the expiry watchdog, its narrowly scoped identity,
    # the retained artifact storage and the EMPTY session resource group that
    # the watchdog is allowed to clean. It shares no state with the platform
    # environment roots, the Foundry capability or the session root, so the
    # session root can be applied and torn down repeatedly while the controls
    # stay in place. See docs/adr/0012-aks-mlops-cost-controls.md.
    use_azuread_auth = true
  }
}
