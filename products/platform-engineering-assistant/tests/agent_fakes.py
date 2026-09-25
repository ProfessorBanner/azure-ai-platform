"""Builders for the Phase 18.1 agent tests. No network, no Azure, no credential."""

from __future__ import annotations

from platform_engineering_assistant.agent.domain import (
    AgentDecision,
    DecisionKind,
    ToolArgument,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.orchestrator import AgentService
from platform_engineering_assistant.agent.protocol import FakeAgentDecisionProvider
from platform_engineering_assistant.agent.registry import ToolRegistry, build_registry
from platform_engineering_assistant.answering import AnsweringService
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.domain import RefusalReason
from platform_engineering_assistant.generation.fake import FakeGenerationProvider
from platform_engineering_assistant.generation.protocol import GenerationProvider
from platform_engineering_assistant.prompts import LoadedPrompt
from platform_engineering_assistant.retrieval.index import build_index
from tests.evaluation_fakes import GENERATION_CONFIG, PROMPT
from tests.generation_fakes import CONFIG, chunk

AGENT_PROMPT = LoadedPrompt(version="agent_decision_v1", content_hash="c" * 64, text="agent prompt")

DEFAULT_CHUNKS = [
    chunk(
        "doc-a::decision::0::0",
        "Terraform state is stored in Azure Blob Storage with one state per environment.",
    ),
    chunk(
        "doc-b::sandbox::0::0",
        "The sandbox Foundry deployment runs at capacity 10 for experimentation.",
        doc_id="adr-0006",
        doc_path="docs/adr/0006-x.md",
    ),
]


def answering_service(
    provider: GenerationProvider | None = None, chunks: list[Chunk] | None = None
) -> AnsweringService:
    """A real AnsweringService over a tiny synthetic corpus."""
    return AnsweringService(
        index=build_index(chunks or DEFAULT_CHUNKS, CONFIG),
        provider=provider or FakeGenerationProvider(),
        prompt=PROMPT,
        retrieval_config=CONFIG,
        generation_config=GENERATION_CONFIG,
        corpus_version=1,
    )


def agent_service(
    decisions: list[AgentDecision] | AgentDecision | None = None,
    *,
    provider: GenerationProvider | None = None,
    chunks: list[Chunk] | None = None,
    registry: ToolRegistry | None = None,
    max_iterations: int = 2,
    approvals: object = None,
    trajectory_sink: object = None,
) -> AgentService:
    """A real AgentService wired to deterministic fakes.

    The REAL service, registry and policy layer — only the two model calls are
    faked, because those are the only parts that would otherwise need Azure.
    """
    answering = answering_service(provider, chunks)
    return AgentService(
        answering=answering,
        registry=registry or build_registry(answering.index),
        decider=FakeAgentDecisionProvider(decisions),
        agent_prompt=AGENT_PROMPT,
        max_iterations=max_iterations,
        approvals=approvals,  # type: ignore[arg-type]
        trajectory_sink=trajectory_sink,  # type: ignore[arg-type]
    )


# --- proposal builders -------------------------------------------------------


def no_tool() -> AgentDecision:
    return AgentDecision(kind=DecisionKind.NO_TOOL)


def refuse(reason: RefusalReason = RefusalReason.OUT_OF_SCOPE) -> AgentDecision:
    return AgentDecision(kind=DecisionKind.REFUSE, refusal_reason=reason)


def use_tool(
    name: str,
    arguments: dict[str, str] | None = None,
    *,
    claimed_risk: ToolRiskLevel | None = None,
) -> AgentDecision:
    return AgentDecision(
        kind=DecisionKind.USE_TOOL,
        tool_name=name,
        # Builders take a readable mapping; the wire encoding is applied here so
        # every test states its intent as arguments rather than as pairs.
        tool_arguments=[ToolArgument(name=k, value=v) for k, v in (arguments or {}).items()],
        claimed_risk_level=claimed_risk,
    )


def search(query: str = "terraform state storage") -> AgentDecision:
    return use_tool("search_platform_docs", {"query": query})


def propose_change(
    title: str = "Raise sandbox capacity",
    rationale: str = "Evaluation runs are throttled.",
    *,
    claimed_risk: ToolRiskLevel | None = None,
) -> AgentDecision:
    """The state-changing proposal. Policy always stops it for approval."""
    return use_tool(
        "propose_change_request",
        {"title": title, "rationale": rationale},
        claimed_risk=claimed_risk,
    )
