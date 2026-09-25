"""Correlation and trajectory validity: what a turn records, and what it must not."""

from __future__ import annotations

import dataclasses
import json

import pytest

from foundry_agent_lab.correlation import (
    FORBIDDEN_FIELDS,
    ToolEvent,
    TurnCorrelation,
    correlate,
    turn_outcome,
    verify,
)
from foundry_agent_lab.governed_runtime import GovernedFoundryLoop
from foundry_agent_lab.protocol import ProposedCall
from foundry_agent_lab.workflow import InMemoryWorkflowStore
from tests.fakes import PROPOSAL, answering, governed_gateway, proposing, runtime

QUESTION = "How is Terraform state separated between environments?"


def run(*responses, agent_version: str = "7"):  # type: ignore[no-untyped-def]
    gateway = governed_gateway()
    loop = GovernedFoundryLoop(
        runtime(*responses),
        gateway,
        InMemoryWorkflowStore(),
        agent_name="lab-agent",
        agent_version=agent_version,
    )
    turn = loop.run(QUESTION)
    return turn, correlate(turn, QUESTION)


# --- 1. the record joins both sides -----------------------------------------


def test_the_record_joins_the_managed_and_application_identifiers() -> None:
    _, record = run(
        proposing(ProposedCall("c1", "search_platform_docs", {"query": "terraform state"})),
        answering(),
    )
    assert record.agent_name == "lab-agent"
    assert record.agent_version == "7"
    assert record.conversation_id
    assert record.response_ids
    assert record.tool_events[0].tool_name == "search_platform_docs"
    assert record.tool_events[0].policy_decision == "allow"
    assert record.outcome == "answered"


def test_an_approval_turn_correlates_the_approval_and_the_workflow() -> None:
    turn, record = run(
        proposing(ProposedCall("c1", "propose_change_request", PROPOSAL)), answering()
    )
    event = record.tool_events[0]
    assert record.outcome == "approval_required"
    assert event.approval_id == turn.approval_ids[0]
    assert len(event.argument_fingerprint or "") == 64
    assert record.workflow_ids


# --- 2. redaction ------------------------------------------------------------


def test_no_correlation_model_declares_a_field_for_content() -> None:
    for model in (TurnCorrelation, ToolEvent):
        declared = {f.name for f in dataclasses.fields(model)}
        assert not declared & FORBIDDEN_FIELDS, f"{model.__name__} declares content fields"


def test_the_question_is_recorded_as_a_length_not_as_text() -> None:
    marker = "PhraseThatOnlyAppearsInTheQuestion"
    gateway = governed_gateway()
    loop = GovernedFoundryLoop(
        runtime(answering()), gateway, InMemoryWorkflowStore(), agent_version="1"
    )
    turn = loop.run(f"What about {marker}?")
    record = correlate(turn, f"What about {marker}?")

    assert marker not in record.as_json()
    assert record.question_chars > 0


def test_the_record_carries_no_instructions_or_credentials() -> None:
    from foundry_agent_lab.governed_runtime import GOVERNED_INSTRUCTIONS

    _, record = run(answering())
    rendered = record.as_json()
    assert GOVERNED_INSTRUCTIONS[:60] not in rendered
    for leak in ("services.ai.azure.com", "api_key", "Bearer ", "DefaultAzureCredential"):
        assert leak not in rendered


def test_no_chain_of_thought_is_collected_anywhere() -> None:
    """It is not collected, so it cannot leak."""
    _, record = run(answering())
    payload = json.loads(record.as_json())
    assert "reasoning" not in payload
    assert "chain_of_thought" not in payload


# --- 3. trajectory validity --------------------------------------------------


def test_a_real_turn_verifies_clean() -> None:
    for responses in (
        (answering(),),
        (
            proposing(ProposedCall("c", "search_platform_docs", {"query": "terraform state"})),
            answering(),
        ),
        (proposing(ProposedCall("c", "propose_change_request", PROPOSAL)), answering()),
    ):
        _, record = run(*responses)
        assert verify(record) == (), f"{record.outcome} produced defects"


def test_a_turn_with_no_provisioned_version_is_a_defect() -> None:
    """Serving against an unprovisioned agent must not look normal."""
    _, record = run(answering(), agent_version="unprovisioned")
    assert "turn_ran_against_no_provisioned_version" in verify(record)


def test_a_state_changing_execution_is_a_defect() -> None:
    """The invariant that must never fire in practice must be ABLE to fire."""
    record = TurnCorrelation(
        turn_id="t",
        agent_name="a",
        agent_version="1",
        conversation_id="c",
        response_ids=("r1",),
        tool_events=(
            ToolEvent(
                tool_name="propose_change_request",
                outcome="executed",
                risk="state_changing",
                policy_decision="allow",
                tool_call_id="tc-1",
            ),
        ),
        outcome="answered",
        question_chars=10,
        final_text_chars=5,
    )
    assert "state_changing_tool_executed" in verify(record)


def test_an_approval_that_also_ran_is_a_defect() -> None:
    record = TurnCorrelation(
        turn_id="t",
        agent_name="a",
        agent_version="1",
        conversation_id="c",
        response_ids=("r1",),
        tool_events=(
            ToolEvent(
                tool_name="propose_change_request",
                outcome="approval_required",
                risk="state_changing",
                approval_id="apr-1",
                argument_fingerprint="f" * 64,
                tool_call_id="tc-1",
            ),
        ),
        outcome="approval_required",
        question_chars=10,
        final_text_chars=0,
        workflow_ids=("wf-1",),
    )
    assert "approval_required_but_a_call_ran" in verify(record)


def test_an_approval_without_a_binding_is_a_defect() -> None:
    record = TurnCorrelation(
        turn_id="t",
        agent_name="a",
        agent_version="1",
        conversation_id="c",
        response_ids=("r1",),
        tool_events=(ToolEvent(tool_name="propose_change_request", outcome="approval_required"),),
        outcome="approval_required",
        question_chars=1,
        final_text_chars=0,
        workflow_ids=("wf-1",),
    )
    assert "approval_without_binding" in verify(record)


@pytest.mark.parametrize(
    ("outcome_field", "expected"),
    [("approval_required", "approval_required"), ("denied", "denied")],
)
def test_turn_outcome_names_how_the_turn_ended(outcome_field: str, expected: str) -> None:
    calls = {
        "approval_required": ProposedCall("c", "propose_change_request", PROPOSAL),
        "denied": ProposedCall("c", "run_terraform_apply", {"environment": "prod"}),
    }
    turn, _ = run(proposing(calls[outcome_field]), answering())
    assert turn_outcome(turn) == expected
