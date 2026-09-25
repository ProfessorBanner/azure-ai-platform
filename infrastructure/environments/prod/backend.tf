terraform {
  backend "azurerm" {
    # Independent state: platform/prod.tfstate (backend/platform-prod.hcl).
    # PROD is the final governed environment; its apply is gated by a manual
    # approval on the azure-ai-platform-prod Azure DevOps Environment.
    use_azuread_auth = true
  }
}
