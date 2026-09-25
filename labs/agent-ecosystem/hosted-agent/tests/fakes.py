"""Builders for the hosted-agent tests. No network, no Azure, no credential."""

from __future__ import annotations

from platform_engineering_assistant.agent.domain import (
    AgentDecision,
    DecisionKind,
    ToolArgument,
)
from platform_engineering_assistant.agent.orchestrator import AgentService, build_agent_service
from platform_engineering_assistant.agent.protocol import FakeAgentDecisionProvider
from platform_engineering_assistant.answering import build_service
from platform_engineering_assistant.generation.fake import FakeGenerationProvider


def no_tool() -> AgentDecision:
    return AgentDecision(kind=DecisionKind.NO_TOOL)


def search(query: str = "terraform state separation") -> AgentDecision:
    return AgentDecision(
        kind=DecisionKind.USE_TOOL,
        tool_name="search_platform_docs",
        tool_arguments=[ToolArgument(name="query", value=query)],
    )


def propose_change() -> AgentDecision:
    return AgentDecision(
        kind=DecisionKind.USE_TOOL,
        tool_name="propose_change_request",
        tool_arguments=[
            ToolArgument(name="title", value="Raise sandbox capacity"),
            ToolArgument(name="rationale", value="Evaluation runs are throttled."),
        ],
    )


def hosted_service(decisions: list[AgentDecision] | AgentDecision | None = None) -> AgentService:
    """The REAL Phase 18 agent over the real corpus, with the deterministic fakes.

    Only the two model calls are faked. The registry, policy layer, executor,
    approval service and bounded loop are the shipped product code — which is
    the point: the hosting adapter must not be able to change any of them.
    """
    answering = build_service(FakeGenerationProvider())
    return build_agent_service(answering, FakeAgentDecisionProvider(decisions))
