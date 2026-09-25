"""The controlled single-agent layer (Phase 18.1).

    LLM             proposes
    Policy layer    authorises or denies
    Tool registry   owns trusted tool metadata, including risk
    Tool            executes
    Application     controls the loop and records the outcome

The model never authorises anything, never learns a tool's risk classification,
and never executes. See docs/adr/0008-controlled-agent-foundation.md.
"""

from platform_engineering_assistant.agent.domain import (
    AgentDecision,
    AgentOutcomeKind,
    AgentRequest,
    AgentResponse,
    DecisionKind,
    DenialReason,
    PolicyDecision,
    PolicyVerdict,
    ToolExecutionStatus,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.orchestrator import (
    AgentService,
    AgentTurn,
    build_agent_service,
)
from platform_engineering_assistant.agent.policy import authorise
from platform_engineering_assistant.agent.protocol import (
    AgentDecisionProvider,
    FakeAgentDecisionProvider,
)
from platform_engineering_assistant.agent.registry import (
    ToolDefinition,
    ToolRegistry,
    build_registry,
)

__all__ = [
    "AgentDecision",
    "AgentDecisionProvider",
    "AgentOutcomeKind",
    "AgentRequest",
    "AgentResponse",
    "AgentService",
    "AgentTurn",
    "DecisionKind",
    "DenialReason",
    "FakeAgentDecisionProvider",
    "PolicyDecision",
    "PolicyVerdict",
    "ToolDefinition",
    "ToolExecutionStatus",
    "ToolRegistry",
    "ToolRiskLevel",
    "authorise",
    "build_agent_service",
    "build_registry",
]
