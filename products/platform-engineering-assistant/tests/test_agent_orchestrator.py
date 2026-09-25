"""The controlled loop: every required Phase 18.1 behaviour, offline."""

from __future__ import annotations

import pytest

from platform_engineering_assistant.agent.domain import (
    AgentOutcomeKind,
    AgentRequest,
    DecisionKind,
    DenialReason,
    PolicyDecision,
    ToolExecutionStatus,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.tools import (
    ProposeChangeInput,
    ProposeChangeRequestTool,
    ToolError,
)
from platform_engineering_assistant.domain import RefusalReason
from platform_engineering_assistant.errors import ProviderError, RateLimitedError
from platform_engineering_assistant.generation.fake import FakeBehaviour, FakeGenerationProvider
from tests.agent_fakes import agent_service, no_tool, refuse, search, use_tool

QUESTION = "How is Terraform state separated between environments?"


def run(service, question: str = QUESTION):  # type: ignore[no-untyped-def]
    return service.run(AgentRequest(question=question))


# --- 1. no-tool grounded answer ---------------------------------------------


def test_a_no_tool_decision_produces_a_grounded_answer() -> None:
    turn = run(agent_service(no_tool()))
    assert turn.response.outcome is AgentOutcomeKind.ANSWERED
    assert turn.response.answer
    assert turn.response.citations
    assert turn.response.selected_tool is None
    assert turn.response.tool_iterations == 0
    assert turn.response.tool_execution_status is ToolExecutionStatus.NOT_EXECUTED


# --- 2 & 3. read-only selection and execution --------------------------------


def test_a_read_only_tool_is_selected_and_allowed() -> None:
    turn = run(agent_service(search()))
    assert turn.response.selected_tool == "search_platform_docs"
    assert turn.response.tool_risk_level is ToolRiskLevel.READ_ONLY
    assert turn.response.policy_decision is PolicyDecision.ALLOW


def test_a_read_only_tool_executes_and_grounds_the_answer() -> None:
    turn = run(agent_service(search()))
    assert turn.response.tool_execution_status is ToolExecutionStatus.SUCCEEDED
    assert turn.response.outcome is AgentOutcomeKind.ANSWERED
    assert turn.response.citations
    assert turn.response.tool_iterations == 2


# --- 4 & 5. denials ----------------------------------------------------------


def test_an_unknown_tool_is_denied_and_nothing_executes() -> None:
    turn = run(agent_service(use_tool("drop_everything")))
    assert turn.response.outcome is AgentOutcomeKind.DENIED
    assert turn.response.denial_reason is DenialReason.UNKNOWN_TOOL
    assert turn.response.tool_execution_status is ToolExecutionStatus.NOT_EXECUTED
    assert turn.response.answer is None


def test_invalid_tool_arguments_are_denied() -> None:
    turn = run(agent_service(use_tool("search_platform_docs", {"q": "too short a key"})))
    assert turn.response.outcome is AgentOutcomeKind.DENIED
    assert turn.response.denial_reason is DenialReason.INVALID_ARGUMENTS
    assert turn.response.tool_execution_status is ToolExecutionStatus.NOT_EXECUTED


# --- 6 & 7. approval, and non-execution --------------------------------------


PROPOSAL = {"title": "Raise sandbox capacity", "rationale": "Evaluation runs are throttled."}


def test_a_state_changing_tool_returns_approval_required() -> None:
    turn = run(agent_service(use_tool("propose_change_request", PROPOSAL)))
    assert turn.response.outcome is AgentOutcomeKind.APPROVAL_REQUIRED
    assert turn.response.policy_decision is PolicyDecision.REQUIRE_APPROVAL
    assert turn.response.tool_risk_level is ToolRiskLevel.STATE_CHANGING


def test_a_state_changing_tool_is_never_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The strongest form: `run` is replaced by a tripwire that fails the test."""
    called: list[str] = []

    def tripwire(self: ProposeChangeRequestTool, payload: ProposeChangeInput) -> None:
        called.append(payload.title)
        raise AssertionError("the state-changing tool was executed")

    monkeypatch.setattr(ProposeChangeRequestTool, "run", tripwire)

    turn = run(agent_service(use_tool("propose_change_request", PROPOSAL)))
    assert called == []
    assert turn.response.outcome is AgentOutcomeKind.APPROVAL_REQUIRED
    assert turn.response.tool_execution_status is ToolExecutionStatus.NOT_EXECUTED


def test_the_approval_summary_is_built_by_the_server_not_the_model() -> None:
    """A human approves text the application controls, assembled from typed input."""
    turn = run(agent_service(use_tool("propose_change_request", PROPOSAL)))
    assert turn.response.approval_summary == "Proposed change: Raise sandbox capacity"


def test_the_proposal_tool_refuses_to_run_even_if_called_directly() -> None:
    """Defence in depth: a refactor that bypassed policy fails loudly."""
    with pytest.raises(ToolError, match="never execute automatically"):
        ProposeChangeRequestTool().run(
            ProposeChangeInput(title="anything at all", rationale="a sufficient rationale")
        )


# --- 8. tool execution failure -----------------------------------------------


def test_a_tool_execution_failure_is_a_controlled_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from platform_engineering_assistant.agent.tools import SearchPlatformDocsTool

    def boom(self: SearchPlatformDocsTool, payload: object) -> None:
        raise ToolError("search backend unavailable")

    monkeypatch.setattr(SearchPlatformDocsTool, "run", boom)

    turn = run(agent_service(search()))
    assert turn.response.outcome is AgentOutcomeKind.FAILED
    assert turn.response.tool_execution_status is ToolExecutionStatus.FAILED
    assert turn.response.answer is None
    assert turn.telemetry.tool_failure_category is not None


# --- 9. iteration bound -------------------------------------------------------


def test_the_loop_never_exceeds_the_iteration_ceiling() -> None:
    turn = run(agent_service(search()))
    assert turn.response.tool_iterations <= 2


def test_a_single_iteration_budget_cannot_reach_the_answering_step() -> None:
    """With no budget to ground on the tool's evidence, the turn refuses."""
    turn = run(agent_service(search(), max_iterations=1))
    assert turn.response.outcome is AgentOutcomeKind.REFUSED
    assert turn.response.tool_iterations == 1


def test_the_ceiling_cannot_be_raised_above_the_hard_limit() -> None:
    service = agent_service(search(), max_iterations=99)
    turn = run(service)
    assert turn.response.tool_iterations <= 2


def test_the_decider_is_called_exactly_once_per_turn() -> None:
    """The loop is bounded by construction, not by a break condition."""
    service = agent_service(search())
    run(service)
    assert len(service._decider.requests) == 1  # type: ignore[attr-defined]


# --- 10. refusal behaviour preserved ------------------------------------------


def test_a_model_refusal_is_preserved() -> None:
    turn = run(agent_service(refuse(RefusalReason.OUT_OF_SCOPE)))
    assert turn.response.outcome is AgentOutcomeKind.REFUSED
    assert turn.response.refusal_reason is RefusalReason.OUT_OF_SCOPE
    assert turn.response.answer is None
    assert not turn.response.citations


@pytest.mark.parametrize(
    "behaviour",
    [
        FakeBehaviour.CITE_UNRETRIEVED_CHUNK,
        FakeBehaviour.CITE_NOTHING,
        FakeBehaviour.CITE_DUPLICATES,
        FakeBehaviour.ANSWER_WITH_EMPTY_TEXT,
    ],
)
def test_fail_closed_grounding_still_governs_tool_grounded_answers(
    behaviour: FakeBehaviour,
) -> None:
    """A tool changes WHICH chunks are considered, never what may be said."""
    service = agent_service(search(), provider=FakeGenerationProvider(behaviour))
    turn = run(service)
    assert turn.response.outcome is AgentOutcomeKind.REFUSED
    assert turn.response.answer is None
    assert not turn.response.citations


def test_every_citation_on_a_tool_grounded_answer_is_server_built() -> None:
    service = agent_service(
        search(), provider=FakeGenerationProvider(FakeBehaviour.ANSWER_ALL_CHUNKS)
    )
    turn = run(service)
    known = set(service._answering.index.chunks)
    known_ids = {chunk.chunk_id for chunk in known}
    assert turn.response.citations
    for citation in turn.response.citations:
        assert citation.chunk_id in known_ids
        assert citation.doc_path


# --- 12. policy cannot be overridden by model output --------------------------


def test_a_model_claiming_a_state_changing_tool_is_read_only_still_needs_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE adversarial case. The registry's classification wins, and the tool
    does not run — no matter what the model asserts about it."""
    called: list[str] = []

    def tripwire(self: ProposeChangeRequestTool, payload: ProposeChangeInput) -> None:
        called.append(payload.title)
        raise AssertionError("the state-changing tool was executed")

    monkeypatch.setattr(ProposeChangeRequestTool, "run", tripwire)

    turn = run(
        agent_service(
            use_tool("propose_change_request", PROPOSAL, claimed_risk=ToolRiskLevel.READ_ONLY)
        )
    )

    assert called == []
    assert turn.response.outcome is AgentOutcomeKind.APPROVAL_REQUIRED
    assert turn.response.tool_risk_level is ToolRiskLevel.STATE_CHANGING
    assert turn.response.tool_execution_status is ToolExecutionStatus.NOT_EXECUTED
    # The false claim is recorded so it is observable, never acted on.
    assert turn.telemetry.claimed_risk_level is ToolRiskLevel.READ_ONLY
    assert turn.telemetry.risk_claim_mismatch is True


def test_an_honest_read_only_claim_is_equally_ignored() -> None:
    """Symmetry proves the field is inert, not merely distrusted when it lies."""
    honest = run(agent_service(search(query := "terraform state")))
    claimed = run(
        agent_service(
            use_tool("search_platform_docs", {"query": query}, claimed_risk=ToolRiskLevel.READ_ONLY)
        )
    )
    assert honest.response.tool_risk_level is claimed.response.tool_risk_level
    assert honest.response.policy_decision is claimed.response.policy_decision


# --- provider failures --------------------------------------------------------


def test_a_decider_failure_propagates_as_a_typed_error() -> None:
    """The agent never invents an answer when it could not obtain a proposal."""
    from platform_engineering_assistant.agent.orchestrator import AgentService
    from platform_engineering_assistant.agent.protocol import FakeAgentDecisionProvider
    from platform_engineering_assistant.agent.registry import build_registry
    from tests.agent_fakes import AGENT_PROMPT, answering_service

    answering = answering_service()
    service = AgentService(
        answering=answering,
        registry=build_registry(answering.index),
        decider=FakeAgentDecisionProvider(error=RateLimitedError("429")),
        agent_prompt=AGENT_PROMPT,
    )
    with pytest.raises(RateLimitedError):
        run(service)


def test_a_generation_failure_during_grounding_propagates() -> None:
    service = agent_service(search(), provider=FakeGenerationProvider(error=ProviderError("5xx")))
    with pytest.raises(ProviderError):
        run(service)


# --- telemetry ---------------------------------------------------------------


def test_telemetry_records_the_decision_without_the_arguments() -> None:
    """Arguments are user-derived content: the count is recorded, never the values."""
    distinctive = "a-very-distinctive-query-string"
    turn = run(agent_service(use_tool("search_platform_docs", {"query": distinctive})))
    rendered = turn.telemetry.summary() + str(turn.telemetry.as_dict())
    assert distinctive not in rendered
    assert turn.telemetry.tool_argument_count == 1


def test_telemetry_carries_the_control_path() -> None:
    turn = run(agent_service(search()))
    assert turn.telemetry.decision_kind is DecisionKind.USE_TOOL
    assert turn.telemetry.selected_tool == "search_platform_docs"
    assert turn.telemetry.policy_decision is PolicyDecision.ALLOW
    assert turn.telemetry.tool_iterations == 2
    assert turn.telemetry.outcome is AgentOutcomeKind.ANSWERED
    assert turn.telemetry.agent_prompt_version == "agent_decision_v1"


def test_a_risk_claim_mismatch_is_surfaced_in_the_log_line() -> None:
    turn = run(
        agent_service(
            use_tool("propose_change_request", PROPOSAL, claimed_risk=ToolRiskLevel.READ_ONLY)
        )
    )
    assert "RISK_CLAIM_MISMATCH" in turn.telemetry.summary()
