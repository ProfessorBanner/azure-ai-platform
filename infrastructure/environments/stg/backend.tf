terraform {
  backend "azurerm" {
    # Independent state: platform/stg.tfstate (backend/platform-stg.hcl).
    # STG is the second governed environment in the dev -> stg -> prod chain.
    use_azuread_auth = true
  }
}
