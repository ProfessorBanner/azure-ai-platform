"""The Phase 18 controlled agent, packaged as a Microsoft Foundry Hosted Agent.

A hosting adapter, not a second agent. The Responses protocol is served by
`azure-ai-agentserver-responses`; orchestration, registry, policy, bounded
execution, approval and audit all remain the product's.
"""

from hosted_agent.config import HostedAgentConfig, HostedConfigurationError, load_hosted_config
from hosted_agent.protocol import metadata_for, text_for
from hosted_agent.server import build_agent, build_host

__all__ = [
    "HostedAgentConfig",
    "HostedConfigurationError",
    "build_agent",
    "build_host",
    "load_hosted_config",
    "metadata_for",
    "text_for",
]
