terraform {
  backend "azurerm" {
    # Independent state: platform/sandbox.tfstate (backend/platform-sandbox.hcl).
    # Sandbox is a separate environment CLASS, not part of the dev/stg/prod
    # promotion chain; its state is isolated from every governed environment.
    use_azuread_auth = true
  }
}
