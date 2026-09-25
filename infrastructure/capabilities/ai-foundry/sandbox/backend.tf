terraform {
  backend "azurerm" {
    # Independent state: capabilities/ai-foundry/sandbox.tfstate
    # (backend/capability-ai-foundry-sandbox.hcl).
    #
    # The Foundry capability lab is deliberately NOT part of any platform
    # environment root. It has its own state so it can be planned, applied and
    # torn down without touching platform/sandbox.tfstate or any governed
    # environment state. See docs/adr/0006-foundry-capability-lab.md.
    use_azuread_auth = true
  }
}
