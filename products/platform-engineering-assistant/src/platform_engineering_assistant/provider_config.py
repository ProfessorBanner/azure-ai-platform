"""Non-secret environment configuration for the Azure OpenAI provider.

There is no secret to configure. The Foundry account runs with
`local_auth_enabled = false`, so a key would be rejected by the service; a
key-shaped environment variable is therefore treated as a configuration ERROR
rather than ignored, so a well-meaning `AZURE_OPENAI_API_KEY` cannot silently
change how the application authenticates.

TWO DIFFERENT ENDPOINTS
-----------------------
  AZURE_OPENAI_ENDPOINT     the MODEL DATA PLANE — an OpenAI-compatible base URL
                            ending `/openai/v1/`. This is what the product calls.
  FOUNDRY_PROJECT_ENDPOINT  RESERVED, never read here. The project-scoped Foundry
                            endpoint used for evaluations and, later, agents.

The generic `<name>.cognitiveservices.azure.com` account endpoint is rejected at
startup: it does not serve the Responses API and would fail with 404 at call
time, after a token had been minted and a request sent.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from platform_engineering_assistant.errors import ConfigurationError

ENDPOINT_VAR = "AZURE_OPENAI_ENDPOINT"
DEPLOYMENT_VAR = "AZURE_OPENAI_DEPLOYMENT"
TIMEOUT_VAR = "AZURE_OPENAI_TIMEOUT_SECONDS"
SCOPE_VAR = "AZURE_OPENAI_AUTH_SCOPE"

PROJECT_ENDPOINT_VAR = "FOUNDRY_PROJECT_ENDPOINT"

# The empirically proven audience for the Foundry /openai/v1/ Responses API.
# Overridable for deliberate diagnostics only; there is no automatic fallback.
DEFAULT_AUTH_SCOPE = "https://ai.azure.com/.default"
DEFAULT_TIMEOUT_SECONDS = 30.0

GENERIC_ACCOUNT_HOST = "cognitiveservices.azure.com"
CANONICAL_ENDPOINT_SUFFIX = "/openai/v1/"

FORBIDDEN_KEY_VARS = (
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "FOUNDRY_API_KEY",
    "AZURE_OPENAI_KEY",
    "COGNITIVE_SERVICES_KEY",
)

_DEPLOYMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class AzureOpenAIConfig:
    """Resolved, validated provider configuration."""

    endpoint: str
    deployment: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    auth_scope: str = DEFAULT_AUTH_SCOPE


def assert_no_api_key_configured(environment: Mapping[str, str] | None = None) -> None:
    """Fail if any API-key variable is set, naming it but never its value."""
    env = os.environ if environment is None else environment
    offenders = sorted(name for name in FORBIDDEN_KEY_VARS if env.get(name))
    if offenders:
        raise ConfigurationError(
            "API-key authentication is not supported and is disabled on the "
            f"Foundry account. Unset {', '.join(offenders)} and use `az login`."
        )


def validate_endpoint(endpoint: str) -> None:
    """Reject anything that is not the canonical model data-plane URL.

    Raises:
        ConfigurationError: naming the rule that failed. The value is never
            echoed: an endpoint identifies the tenant's resource.
    """
    if not endpoint:
        raise ConfigurationError(f"{ENDPOINT_VAR} is not set.")
    if not endpoint.startswith("https://"):
        raise ConfigurationError(f"{ENDPOINT_VAR} must be an https:// URL.")
    if GENERIC_ACCOUNT_HOST in endpoint:
        raise ConfigurationError(
            f"{ENDPOINT_VAR} points at the generic Cognitive Services account host "
            f"({GENERIC_ACCOUNT_HOST}), which does not serve the Responses API and "
            f"404s at call time. Use the canonical data-plane URL ending "
            f"{CANONICAL_ENDPOINT_SUFFIX} — the `llm_endpoint` Terraform output."
        )
    if not endpoint.endswith(CANONICAL_ENDPOINT_SUFFIX):
        raise ConfigurationError(
            f"{ENDPOINT_VAR} must end with '{CANONICAL_ENDPOINT_SUFFIX}' (trailing slash included)."
        )


def load_provider_config(environment: Mapping[str, str] | None = None) -> AzureOpenAIConfig:
    """Build provider configuration from the environment.

    Raises:
        ConfigurationError: on a missing or malformed value, or a configured key.
    """
    env = os.environ if environment is None else environment

    assert_no_api_key_configured(env)

    endpoint = (env.get(ENDPOINT_VAR) or "").strip()
    validate_endpoint(endpoint)

    deployment = (env.get(DEPLOYMENT_VAR) or "").strip()
    if not deployment:
        raise ConfigurationError(f"{DEPLOYMENT_VAR} is not set.")
    if not _DEPLOYMENT_PATTERN.match(deployment):
        raise ConfigurationError(f"{DEPLOYMENT_VAR} is not a valid deployment name.")

    raw_timeout = (env.get(TIMEOUT_VAR) or "").strip()
    if raw_timeout:
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise ConfigurationError(f"{TIMEOUT_VAR} must be a number.") from exc
        if timeout_seconds <= 0:
            raise ConfigurationError(f"{TIMEOUT_VAR} must be greater than zero.")
    else:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    auth_scope = (env.get(SCOPE_VAR) or "").strip() or DEFAULT_AUTH_SCOPE

    return AzureOpenAIConfig(
        endpoint=endpoint,
        deployment=deployment,
        timeout_seconds=timeout_seconds,
        auth_scope=auth_scope,
    )
