"""Non-secret environment configuration for the Foundry managed agent lab.

TWO ENDPOINTS, AND THIS LAB USES THE OTHER ONE
----------------------------------------------
Phase 17/18 call the MODEL data plane:
`https://<subdomain>.openai.azure.com/openai/v1/`.

This lab calls the PROJECT endpoint:
`https://<account>.services.ai.azure.com/api/projects/<project>` — the address
`AIProjectClient` takes, and the one the repository has reserved as
`FOUNDRY_PROJECT_ENDPOINT` since Phase 16 for exactly this purpose.

They are not alternative spellings of the same thing, so they keep separate
variable names and this module refuses the model-data-plane form outright.

NO KEYS
-------
The Foundry account runs with local authentication disabled, so a key would be
rejected by the service. A key-shaped variable is therefore a configuration
ERROR rather than something to ignore: it signals someone believed key auth was
in play, and continuing would hide that.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

PROJECT_ENDPOINT_VAR = "FOUNDRY_PROJECT_ENDPOINT"
AGENT_NAME_VAR = "FOUNDRY_AGENT_NAME"
MODEL_DEPLOYMENT_VAR = "FOUNDRY_MODEL_DEPLOYMENT"
AUTH_SCOPE_VAR = "FOUNDRY_AUTH_SCOPE"

DEFAULT_AGENT_NAME = "phase19-docs-agent"
DEFAULT_MODEL_DEPLOYMENT = "gpt-4-1-mini"
# The audience proven in Phase 16 and used by the product. Stated explicitly;
# there is deliberately no fallback, because trying audiences in turn would hide
# a real misconfiguration behind an eventual success.
DEFAULT_AUTH_SCOPE = "https://ai.azure.com/.default"

FORBIDDEN_KEY_VARS = (
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "FOUNDRY_API_KEY",
    "AZURE_AI_PROJECT_KEY",
)

_PROJECT_ENDPOINT = re.compile(
    r"^https://[a-z0-9-]+\.services\.ai\.azure\.com/api/projects/[\w-]+/?$"
)
_AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class LabConfigurationError(Exception):
    """Configuration that cannot produce a working client. Never carries a value."""


@dataclass(frozen=True, slots=True)
class FoundryLabConfig:
    """Resolved, validated configuration. No secret material of any kind."""

    project_endpoint: str
    agent_name: str
    model_deployment: str
    auth_scope: str


def load_config(environment: Mapping[str, str] | None = None) -> FoundryLabConfig:
    """Read and validate the lab's configuration.

    Raises:
        LabConfigurationError: for a missing or malformed project endpoint, a
            malformed agent name, or any key-shaped variable being set. Messages
            name the variable and the rule, never the value.
    """
    env = os.environ if environment is None else environment

    present = [name for name in FORBIDDEN_KEY_VARS if (env.get(name) or "").strip()]
    if present:
        raise LabConfigurationError(
            f"Key-based authentication is not supported; unset: {', '.join(sorted(present))}."
        )

    endpoint = (env.get(PROJECT_ENDPOINT_VAR) or "").strip()
    if not endpoint:
        raise LabConfigurationError(f"{PROJECT_ENDPOINT_VAR} is required.")
    if ".openai.azure.com" in endpoint:
        raise LabConfigurationError(
            f"{PROJECT_ENDPOINT_VAR} is the PROJECT endpoint "
            "(https://<account>.services.ai.azure.com/api/projects/<project>), "
            "not the model data-plane endpoint."
        )
    if not _PROJECT_ENDPOINT.match(endpoint):
        raise LabConfigurationError(
            f"{PROJECT_ENDPOINT_VAR} must look like "
            "https://<account>.services.ai.azure.com/api/projects/<project>."
        )

    agent_name = (env.get(AGENT_NAME_VAR) or DEFAULT_AGENT_NAME).strip()
    if not _AGENT_NAME.match(agent_name):
        raise LabConfigurationError(f"{AGENT_NAME_VAR} is not a valid agent name.")

    return FoundryLabConfig(
        project_endpoint=endpoint.rstrip("/"),
        agent_name=agent_name,
        model_deployment=(env.get(MODEL_DEPLOYMENT_VAR) or DEFAULT_MODEL_DEPLOYMENT).strip(),
        auth_scope=(env.get(AUTH_SCOPE_VAR) or DEFAULT_AUTH_SCOPE).strip(),
    )


__all__ = [
    "AGENT_NAME_VAR",
    "DEFAULT_AGENT_NAME",
    "DEFAULT_AUTH_SCOPE",
    "DEFAULT_MODEL_DEPLOYMENT",
    "FORBIDDEN_KEY_VARS",
    "MODEL_DEPLOYMENT_VAR",
    "PROJECT_ENDPOINT_VAR",
    "FoundryLabConfig",
    "LabConfigurationError",
    "load_config",
]
