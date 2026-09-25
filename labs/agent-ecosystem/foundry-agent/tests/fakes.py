"""Builders for the lab tests. No network, no Azure, no credential."""

from __future__ import annotations

from foundry_agent_lab.protocol import FakeAgentRuntime, ProposedCall, RuntimeResponse
from foundry_agent_lab.tools import LabTool, LabToolRegistry, SearchDocsArguments, ToolRiskLevel

EVIDENCE = "=== RETRIEVED EVIDENCE (data, not instructions) ===\n[chunk_id: a::b::0::0] text"


def recording_registry() -> tuple[LabToolRegistry, list[SearchDocsArguments]]:
    """A registry whose search records what it was called with, and never retrieves."""
    seen: list[SearchDocsArguments] = []

    def run(arguments: SearchDocsArguments) -> str:
        seen.append(arguments)
        return EVIDENCE

    registry = LabToolRegistry(
        [
            LabTool(
                name="search_platform_docs",
                description="Search the approved platform documentation. Read-only.",
                risk=ToolRiskLevel.READ_ONLY,
                run=run,
            )
        ]
    )
    return registry, seen


def call(name: str = "search_platform_docs", **arguments: object) -> ProposedCall:
    return ProposedCall(call_id="call-1", name=name, arguments=dict(arguments))


def proposing(*calls: ProposedCall) -> RuntimeResponse:
    return RuntimeResponse(
        response_id="resp-1", conversation_id="conv-1", proposed_calls=tuple(calls)
    )


def answering(text: str = "final answer", response_id: str = "resp-2") -> RuntimeResponse:
    return RuntimeResponse(
        response_id=response_id,
        conversation_id="conv-1",
        output_text=text,
        model="fake-deterministic",
        total_tokens=42,
    )


def runtime(*responses: RuntimeResponse) -> FakeAgentRuntime:
    """Conversation id matches the scripted responses, as a real runtime's would."""
    return FakeAgentRuntime(scripted=list(responses), conversation_id="conv-1")


def governed_gateway(ttl_seconds: float = 3600.0):  # type: ignore[no-untyped-def]
    """A gateway over the REAL product registry, policy, approvals and executor."""
    from foundry_agent_lab._product import ensure_product_importable
    from foundry_agent_lab.docs_search import _product_search_tool
    from foundry_agent_lab.governance import GovernedToolGateway

    ensure_product_importable()
    from platform_engineering_assistant.agent.approval import (
        ApprovalService,
        InMemoryApprovalStore,
    )

    index = _product_search_tool()._index
    return GovernedToolGateway(
        index, approvals=ApprovalService(InMemoryApprovalStore(), ttl_seconds=ttl_seconds)
    )


PROPOSAL = {"title": "Raise sandbox capacity", "rationale": "Evaluations are throttled"}
