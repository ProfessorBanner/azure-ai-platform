"""Rendering a Phase 18 outcome: the text, and the structured metadata.

The Responses protocol itself is the library's and is not tested here — testing
someone else's SSE lifecycle would be testing the wrong thing. What IS tested is
the part this package owns: that an outcome survives the trip as something a
caller can branch on, and that prose never claims an action was performed.
"""

from __future__ import annotations

from platform_engineering_assistant.agent.domain import (
    AgentOutcomeKind,
    AgentResponse,
    DenialReason,
    PolicyDecision,
    ToolRiskLevel,
)
from platform_engineering_assistant.domain import Citation, ModelMetadata, RefusalReason

from hosted_agent.protocol import metadata_for, text_for


def response(**overrides: object) -> AgentResponse:
    base: dict[str, object] = {
        "request_id": "req-1",
        "outcome": AgentOutcomeKind.ANSWERED,
        "answer": "Terraform state is separated per environment.",
        # An answered response must carry a citation — the Phase 18 grounding
        # invariant. The fixture honours it rather than relaxing it.
        "citations": [
            Citation(
                chunk_id="adr-0002::decision::0::0",
                doc_id="adr-0002",
                doc_path="docs/adr/0002-terraform-state-backend.md",
                score=1.0,
            )
        ],
        "prompt_version": "answer_v1",
        "agent_prompt_version": "agent_decision_v1",
        "retrieval_config_version": "retrieval_v1",
        "corpus_version": 1,
        "model_metadata": ModelMetadata(provider="fake"),
        "latency_ms": 1.0,
    }
    base.update(overrides)
    if base["outcome"] is not AgentOutcomeKind.ANSWERED:
        base["answer"] = None
        base["citations"] = []
    return AgentResponse(**base)


def test_an_answered_turn_carries_its_text() -> None:
    assert "separated per environment" in text_for(response())
    assert metadata_for(response())["outcome"] == "answered"


def test_an_approval_required_turn_never_claims_the_action_was_carried_out() -> None:
    """THE translation risk: prose that reads as success."""
    approval = response(
        outcome=AgentOutcomeKind.APPROVAL_REQUIRED,
        policy_decision=PolicyDecision.REQUIRE_APPROVAL,
        approval_id="apr-1",
        approval_summary="Proposed change: Raise sandbox capacity",
        selected_tool="propose_change_request",
        tool_risk_level=ToolRiskLevel.STATE_CHANGING,
    )
    assert "NOT been carried out" in text_for(approval)

    metadata = metadata_for(approval)
    assert metadata["outcome"] == "approval_required"
    assert metadata["approval_id"] == "apr-1"
    assert metadata["tool_execution_status"] == "not_executed"
    assert metadata["tool_risk_level"] == "state_changing"


def test_every_outcome_is_machine_readable_not_only_prose() -> None:
    """A caller must be able to branch without parsing English."""
    cases = [
        (AgentOutcomeKind.REFUSED, {"refusal_reason": RefusalReason.OUT_OF_SCOPE}),
        (AgentOutcomeKind.DENIED, {"denial_reason": DenialReason.UNKNOWN_TOOL}),
        (AgentOutcomeKind.FAILED, {}),
    ]
    for outcome, extra in cases:
        metadata = metadata_for(response(outcome=outcome, **extra))
        assert metadata["outcome"] == outcome.value


def test_metadata_is_a_flat_string_map() -> None:
    """Response metadata is a string map by convention; nested values would be
    dropped or rejected by the platform."""
    for value in metadata_for(response()).values():
        assert isinstance(value, str)


def test_metadata_carries_provenance_for_audit() -> None:
    metadata = metadata_for(response())
    assert metadata["agent_prompt_version"] == "agent_decision_v1"
    assert metadata["prompt_version"] == "answer_v1"
    assert metadata["corpus_version"] == "1"
    assert metadata["request_id"] == "req-1"


def test_metadata_carries_no_answer_text_or_evidence() -> None:
    """The same redaction posture the product's telemetry keeps."""
    rendered = " ".join(metadata_for(response()).values())
    assert "separated per environment" not in rendered
