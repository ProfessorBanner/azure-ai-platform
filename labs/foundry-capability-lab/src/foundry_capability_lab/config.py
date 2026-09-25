"""Lab configuration, read from non-secret environment variables.

There is no secret to configure. The Foundry account runs with
`local_auth_enabled = false`, so an API key is not merely discouraged here — it
does not exist and would be rejected by the service. The loader treats any
key-shaped environment variable as a configuration ERROR rather than ignoring
it, so a well-meaning `AZURE_OPENAI_API_KEY` cannot silently change how the lab
authenticates.

TWO DIFFERENT ENDPOINTS, DELIBERATELY NAMED APART
-------------------------------------------------
A Foundry resource publishes more than one address, and they serve different
APIs. The variables below are prefixed `AZURE_OPENAI_` precisely so the one this
lab uses cannot be confused with the other:

  AZURE_OPENAI_ENDPOINT       the MODEL DATA PLANE. An OpenAI-compatible base
                              URL ending `/openai/v1/`, implementing the
                              Responses API. This is what the lab calls, and the
                              only endpoint it knows how to call.

  FOUNDRY_PROJECT_ENDPOINT    RESERVED, not read by this lab. The project-scoped
                              Foundry endpoint (…services.ai.azure.com/api/
                              projects/<project>) used by the Foundry SDK for
                              project operations, evaluations and, later, agents.
                              Phase 16.3+ will introduce it under this name.

Pointing AZURE_OPENAI_ENDPOINT at the project endpoint will fail: they are not
alternative spellings of one address.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from foundry_capability_lab.errors import ConfigurationError

ENDPOINT_VAR = "AZURE_OPENAI_ENDPOINT"
DEPLOYMENT_VAR = "AZURE_OPENAI_DEPLOYMENT"
TIMEOUT_VAR = "AZURE_OPENAI_TIMEOUT_SECONDS"
SCOPE_VAR = "AZURE_OPENAI_AUTH_SCOPE"

# Reserved terminology. This lab never reads it; it is named here so the project
# endpoint has one agreed name across the repository before Phase 16.3 needs it.
PROJECT_ENDPOINT_VAR = "FOUNDRY_PROJECT_ENDPOINT"

# The Entra ID token audience for the Foundry `/openai/v1/` data-plane API.
#
# The audience is a property of the ENDPOINT AND API CONTRACT being called, not
# of the caller or of Azure generally: a token minted for one audience is not
# accepted by another. This value is the correct, explicitly chosen audience for
# the new Foundry Responses API surface that AZURE_OPENAI_ENDPOINT addresses.
#
# It is overridable through AZURE_OPENAI_AUTH_SCOPE purely as a DIAGNOSTIC
# escape hatch — for example when deliberately testing against a different API
# surface. The lab does NOT try audiences in turn and does not fall back: a
# wrong audience must surface as a clean 401 that names the cause, because
# silently retrying with another scope would hide a real misconfiguration and
# make the failure mode unlearnable.
DEFAULT_AUTH_SCOPE = "https://ai.azure.com/.default"

DEFAULT_TIMEOUT_SECONDS = 30.0

# Environment variables that would indicate someone is trying to authenticate
# with a key. Their presence is always a mistake against this account.
FORBIDDEN_KEY_VARS = (
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "FOUNDRY_API_KEY",
    "AZURE_OPENAI_KEY",
    "COGNITIVE_SERVICES_KEY",
)


@dataclass(frozen=True, slots=True)
class LabConfig:
    """Resolved, validated configuration for one lab run."""

    endpoint: str
    deployment: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    auth_scope: str = DEFAULT_AUTH_SCOPE


def assert_no_api_key_configured(environment: Mapping[str, str] | None = None) -> None:
    """Fail if any API-key environment variable is set.

    Raises:
        ConfigurationError: naming only the OFFENDING VARIABLE, never its value.
    """
    env = os.environ if environment is None else environment
    offenders = sorted(name for name in FORBIDDEN_KEY_VARS if env.get(name))
    if offenders:
        raise ConfigurationError(
            "API-key authentication is not supported by this lab and is disabled "
            "on the Foundry account (local_auth_enabled = false). Unset "
            f"{', '.join(offenders)} and authenticate with `az login` instead."
        )


# The generic account host. It answers for every Cognitive Services API on the
# resource and does NOT serve the OpenAI Responses API, so pointing the lab at it
# produces a 404 at call time — after a token has been minted and a request sent.
# Rejecting it during configuration turns a confusing runtime failure into a
# precise startup message.
GENERIC_ACCOUNT_HOST = "cognitiveservices.azure.com"

# The canonical data-plane suffix. The OpenAI-compatible v1 surface is served
# only beneath this path.
CANONICAL_ENDPOINT_SUFFIX = "/openai/v1/"


def validate_endpoint(endpoint: str) -> None:
    """Reject anything that is not the canonical model data-plane URL.

    A Foundry account publishes dozens of endpoints (`properties.endpoints` on
    the account lists more than sixty). Three matter here:

      properties.endpoint
          `https://<subdomain>.cognitiveservices.azure.com/` — the GENERIC
          Cognitive Services account endpoint. It is the value most obviously
          available in the portal and from `az cognitiveservices account show`,
          and it is the wrong one: it does not serve the OpenAI Responses API.

      properties.endpoints["OpenAI Language Model Instance API"]
          `https://<subdomain>.openai.azure.com/` — the correct OpenAI HOST,
          but only the host. It still needs the `/openai/v1/` path appended.

      Terraform output `llm_endpoint`
          `https://<subdomain>.openai.azure.com/openai/v1/` — the canonical,
          complete URL. Read it from Terraform rather than assembling it by hand.

    Raises:
        ConfigurationError: naming the rule that failed. The offending value is
            never echoed: an endpoint is not a secret, but it identifies the
            tenant's resource and has no business in a log line.
    """
    if not endpoint:
        raise ConfigurationError(f"{ENDPOINT_VAR} is not set.")

    if not endpoint.startswith("https://"):
        raise ConfigurationError(f"{ENDPOINT_VAR} must be an https:// URL.")

    if GENERIC_ACCOUNT_HOST in endpoint:
        raise ConfigurationError(
            f"{ENDPOINT_VAR} points at the generic Cognitive Services account host "
            f"({GENERIC_ACCOUNT_HOST}), which does not serve the OpenAI Responses "
            f"API and would fail with 404 at call time. Use the canonical "
            f"data-plane URL instead: the `llm_endpoint` Terraform output, of the "
            f"form https://<subdomain>.openai.azure.com{CANONICAL_ENDPOINT_SUFFIX}"
        )

    if not endpoint.endswith(CANONICAL_ENDPOINT_SUFFIX):
        raise ConfigurationError(
            f"{ENDPOINT_VAR} must end with '{CANONICAL_ENDPOINT_SUFFIX}' (note the "
            f"trailing slash). A bare OpenAI host is not enough: the v1 surface is "
            f"served beneath that path. Use the `llm_endpoint` Terraform output."
        )


def load_config(environment: Mapping[str, str] | None = None) -> LabConfig:
    """Build a LabConfig from the environment.

    Raises:
        ConfigurationError: if a required variable is missing or malformed, or
            if an API key is configured.
    """
    env = os.environ if environment is None else environment

    assert_no_api_key_configured(env)

    endpoint = (env.get(ENDPOINT_VAR) or "").strip()
    validate_endpoint(endpoint)

    deployment = (env.get(DEPLOYMENT_VAR) or "").strip()
    if not deployment:
        raise ConfigurationError(f"{DEPLOYMENT_VAR} is not set.")

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

    return LabConfig(
        endpoint=endpoint,
        deployment=deployment,
        timeout_seconds=timeout_seconds,
        auth_scope=auth_scope,
    )
