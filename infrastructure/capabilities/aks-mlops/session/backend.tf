terraform {
  backend "azurerm" {
    # Independent state: capabilities/aks-mlops/session.tfstate
    # (backend/capability-aks-mlops-session.hcl).
    #
    # The DISPOSABLE session root: one AKS lab cluster and its transport
    # registry, deployed into the empty session resource group the controls
    # root owns. Separate state so it can be created and destroyed per session
    # without touching the controls, the retained storage or any platform
    # environment. See docs/adr/0012-aks-mlops-cost-controls.md.
    use_azuread_auth = true
  }
}
