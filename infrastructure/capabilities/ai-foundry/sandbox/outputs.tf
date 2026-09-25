# ---------------------------------------------------------------------------
# Operational outputs (platform engineering view).
# ---------------------------------------------------------------------------

output "foundry_account_id" {
  description = "Resource ID of the Foundry account. Use this as the scope for runtime role assignments."
  value       = module.ai_foundry.account_id
}

output "foundry_account_name" {
  description = "Name of the Foundry account."
  value       = module.ai_foundry.account_name
}

output "foundry_account_endpoint" {
  description = "Cognitive Services data-plane endpoint of the Foundry account."
  value       = module.ai_foundry.account_endpoint
}

output "foundry_account_principal_id" {
  description = "Principal ID of the account's system-assigned managed identity."
  value       = module.ai_foundry.account_principal_id
}

output "foundry_project_id" {
  description = "Resource ID of the Foundry project."
  value       = module.ai_foundry.project_id
}

output "foundry_project_name" {
  description = "Name of the Foundry project."
  value       = module.ai_foundry.project_name
}

output "foundry_project_principal_id" {
  description = "Principal ID of the project's system-assigned managed identity."
  value       = module.ai_foundry.project_principal_id
}

output "foundry_project_endpoints" {
  description = "Map of endpoints published by the Foundry project, keyed by API surface. Resolved by Azure at apply time; the exact keys are confirmed after the first apply."
  value       = module.ai_foundry.project_endpoints
}

output "foundry_deployment_name" {
  description = "Name of the model deployment."
  value       = module.ai_foundry.deployment_name
}

output "foundry_model_name" {
  description = "Name of the deployed model."
  value       = module.ai_foundry.model_name
}

output "foundry_model_version" {
  description = "Pinned version of the deployed model."
  value       = module.ai_foundry.model_version
}

output "foundry_model_format" {
  description = "Publisher format of the deployed model."
  value       = module.ai_foundry.model_format
}

output "foundry_deployment_sku_name" {
  description = "SKU of the model deployment. Standard is regional; GlobalStandard routes across Microsoft's global fleet."
  value       = module.ai_foundry.deployment_sku_name
}

output "foundry_deployment_capacity" {
  description = "Provisioned capacity of the deployment, in units of 1,000 tokens per minute."
  value       = module.ai_foundry.deployment_capacity
}

output "foundry_version_upgrade_option" {
  description = "How Azure handles new model versions for this deployment."
  value       = module.ai_foundry.version_upgrade_option
}

output "foundry_diagnostic_setting_id" {
  description = "Resource ID of the diagnostic setting streaming account telemetry to the sandbox Log Analytics workspace."
  value       = module.ai_foundry.diagnostic_setting_id
}

output "foundry_local_auth_enabled" {
  description = "Whether API-key authentication is permitted. False means Microsoft Entra ID is the only accepted credential and no secret exists."
  value       = module.ai_foundry.local_auth_enabled
}

output "foundry_network_mode" {
  description = "Human-readable summary of the account's network reachability posture."
  value       = module.ai_foundry.public_network_access_enabled ? "public-endpoint-ip-allowlist-default-${lower(module.ai_foundry.network_acls_default_action)}" : "private-only"
}

# ---------------------------------------------------------------------------
# Phase 17 consumer contract.
#
# The minimal, stable surface a downstream application needs. It is deliberately
# provider-shaped rather than Foundry-shaped: a base URL, a model identifier, an
# Entra scope and an API contract name. Phase 17 maps these onto its own
# FoundryLLMProvider adapter and must not leak Foundry, Cognitive Services or
# Azure vocabulary into its domain or API layers.
#
# No secret is exposed here, and none exists: the account runs with
# local_auth_enabled = false, so callers authenticate with an Entra ID token
# obtained from their own managed identity or developer credential.
# ---------------------------------------------------------------------------

output "llm_endpoint" {
  description = "OpenAI-compatible v1 base URL for the Responses API. Clients append the API path (for example `responses`) to this base."
  value       = "https://${module.ai_foundry.account_custom_subdomain_name}.openai.azure.com/openai/v1/"
}

output "llm_project_endpoint" {
  description = "Foundry project endpoint, if the project publishes one. Resolved from the project endpoints map at apply time; null when no project-scoped endpoint is published."
  value = try(
    coalesce(
      lookup(module.ai_foundry.project_endpoints, "AI Foundry API", null),
      lookup(module.ai_foundry.project_endpoints, "OpenAI", null),
    ),
    null
  )
}

output "llm_deployment" {
  description = "Deployment identifier a client sends as its model."
  value       = module.ai_foundry.deployment_name
}

output "llm_auth_scope" {
  description = "Microsoft Entra ID token scope for data-plane calls."
  value       = "https://ai.azure.com/.default"
}

output "llm_api_contract" {
  description = "API contract the endpoint implements. Consumers must target the OpenAI-compatible Responses API v1, not Chat Completions."
  value       = "openai-responses-v1"
}

# --- Container registry (Phase 19.1e Hosted Agent) ---------------------------

output "container_registry_name" {
  description = "Name of the container registry holding the Hosted Agent image."
  value       = azurerm_container_registry.hosted_agent.name
}

output "container_registry_login_server" {
  description = "Login server of the container registry; the host portion of an image reference."
  value       = azurerm_container_registry.hosted_agent.login_server
}

output "container_registry_id" {
  description = "Resource ID of the container registry. Used as the scope of the Foundry project's pull role assignment."
  value       = azurerm_container_registry.hosted_agent.id
}
