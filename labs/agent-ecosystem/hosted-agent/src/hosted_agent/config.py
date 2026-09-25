"""Environment configuration for the hosted agent process.

Everything the container needs arrives as an environment variable, because that
is what `HostedAgentDefinition.environment_variables` supplies and because a
hosted process has no other configuration surface it controls.

NO SECRETS ARE READ HERE. The container authenticates to Azure OpenAI with the
managed identity Foundry gives it, through the product's existing
`DefaultAzureCredential` path. A key-shaped variable is a configuration error,
exactly as it is in the product: the Foundry account has local authentication
disabled, so a key signals a wrong belief rather than an alternative.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

PORT_VAR = "PORT"
AGENT_NAME_VAR = "HOSTED_AGENT_NAME"
MAX_BODY_VAR = "HOSTED_AGENT_MAX_INPUT_CHARS"

DEFAULT_PORT = 8088
DEFAULT_AGENT_NAME = "phase19-hosted-controlled-agent"
DEFAULT_MAX_INPUT_CHARS = 4000

FORBIDDEN_KEY_VARS = (
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "FOUNDRY_API_KEY",
)


class HostedConfigurationError(Exception):
    """Configuration that cannot produce a working process. Never carries a value."""


@dataclass(frozen=True, slots=True)
class HostedAgentConfig:
    """Resolved configuration. No secret material of any kind."""

    port: int
    agent_name: str
    max_input_chars: int


def load_hosted_config(environment: Mapping[str, str] | None = None) -> HostedAgentConfig:
    """Read and validate the process configuration.

    Raises:
        HostedConfigurationError: for a non-numeric or out-of-range port, a
            meaningless input bound, or any key-shaped variable being set.
    """
    env = os.environ if environment is None else environment

    present = [name for name in FORBIDDEN_KEY_VARS if (env.get(name) or "").strip()]
    if present:
        raise HostedConfigurationError(
            f"Key-based authentication is not supported; unset: {', '.join(sorted(present))}."
        )

    raw_port = (env.get(PORT_VAR) or "").strip()
    port = DEFAULT_PORT
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise HostedConfigurationError(f"{PORT_VAR} must be a whole number.") from exc
        if not 1 <= port <= 65535:
            raise HostedConfigurationError(f"{PORT_VAR} must be between 1 and 65535.")

    raw_max = (env.get(MAX_BODY_VAR) or "").strip()
    max_input = DEFAULT_MAX_INPUT_CHARS
    if raw_max:
        try:
            max_input = int(raw_max)
        except ValueError as exc:
            raise HostedConfigurationError(f"{MAX_BODY_VAR} must be a whole number.") from exc
        if max_input < 1:
            raise HostedConfigurationError(f"{MAX_BODY_VAR} must be positive.")

    return HostedAgentConfig(
        port=port,
        agent_name=(env.get(AGENT_NAME_VAR) or DEFAULT_AGENT_NAME).strip(),
        max_input_chars=max_input,
    )


__all__ = [
    "AGENT_NAME_VAR",
    "DEFAULT_AGENT_NAME",
    "DEFAULT_MAX_INPUT_CHARS",
    "DEFAULT_PORT",
    "FORBIDDEN_KEY_VARS",
    "MAX_BODY_VAR",
    "PORT_VAR",
    "HostedAgentConfig",
    "HostedConfigurationError",
    "load_hosted_config",
]
