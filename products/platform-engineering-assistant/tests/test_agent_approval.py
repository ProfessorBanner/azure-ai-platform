"""Phase 18.3: approval as a deterministic control before any consequential action."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from platform_engineering_assistant.agent.approval import (
    ApprovalError,
    ApprovalReason,
    ApprovalService,
    ApprovalStatus,
    InMemoryApprovalStore,
    is_machine_principal,
)
from platform_engineering_assistant.agent.domain import AgentOutcomeKind, AgentRequest
from platform_engineering_assistant.agent.execution import (
    ExecutionStatus,
    RetryPolicy,
    ToolExecutor,
)
from platform_engineering_assistant.agent.protocol import FakeAgentDecisionProvider
from platform_engineering_assistant.agent.tools import ProposeChangeInput
from platform_engineering_assistant.api.app import create_app
from platform_engineering_assistant.generation.fake import FakeGenerationProvider
from tests.agent_fakes import agent_service, use_tool
from tests.test_agent_execution import RecordingProposal, definition_for

QUESTION = "Should sandbox capacity be raised?"
PROPOSAL_ARGS = {"title": "Raise sandbox capacity", "rationale": "Evaluation runs are throttled."}
PAYLOAD = ProposeChangeInput(
    title="Raise sandbox capacity", rationale="Evaluation runs are throttled."
)
HUMAN = "bruce.banner@example.com"


def service(ttl_seconds: float = 3600.0) -> ApprovalService:
    return ApprovalService(InMemoryApprovalStore(), ttl_seconds=ttl_seconds)


def pending(svc: ApprovalService, requested_by: str = "agt-origin") -> str:
    return svc.request(
        tool_name="propose_change_request",
        payload=PAYLOAD,
        summary="Proposed change: Raise sandbox capacity",
        requested_by=requested_by,
    ).approval_id


# --- 1. approval required -----------------------------------------------------


def test_a_state_changing_proposal_creates_a_pending_approval_and_stops() -> None:
    svc = service()
    turn = agent_service(use_tool("propose_change_request", PROPOSAL_ARGS), approvals=svc).run(
        AgentRequest(question=QUESTION)
    )

    assert turn.response.outcome is AgentOutcomeKind.APPROVAL_REQUIRED
    assert turn.response.approval_id
    record = svc.store.get(turn.response.approval_id)
    assert record is not None
    assert record.status is ApprovalStatus.PENDING


def test_the_approval_records_the_exact_tool_and_arguments() -> None:
    """Approving 'a change request' rather than a specific one lets the
    arguments be swapped after the fact."""
    svc = service()
    approval_id = pending(svc)
    record = svc.store.get(approval_id)
    assert record is not None
    assert record.action.tool_name == "propose_change_request"
    assert len(record.action.argument_fingerprint) == 64


# --- 2 & 3. approved and denied execution -------------------------------------


def test_an_approved_action_may_execute() -> None:
    svc = service()
    approval_id = pending(svc)
    svc.decide(approval_id, approved=True, approver=HUMAN)

    decision = svc.authorise_execution(
        approval_id, tool_name="propose_change_request", payload=PAYLOAD
    )
    assert decision.permitted is True
    assert decision.status is ApprovalStatus.APPROVED


def test_a_denied_action_may_not_execute() -> None:
    svc = service()
    approval_id = pending(svc)
    svc.decide(approval_id, approved=False, approver=HUMAN)

    decision = svc.authorise_execution(
        approval_id, tool_name="propose_change_request", payload=PAYLOAD
    )
    assert decision.permitted is False
    assert decision.status is ApprovalStatus.DENIED
    assert decision.reason is ApprovalReason.NOT_APPROVED


def test_a_pending_action_may_not_execute() -> None:
    """Only an explicit approved state permits execution."""
    svc = service()
    decision = svc.authorise_execution(
        pending(svc), tool_name="propose_change_request", payload=PAYLOAD
    )
    assert decision.permitted is False
    assert decision.status is ApprovalStatus.PENDING


def test_an_unknown_approval_may_not_execute() -> None:
    decision = service().authorise_execution(
        "apr-does-not-exist", tool_name="propose_change_request", payload=PAYLOAD
    )
    assert decision.permitted is False
    assert decision.reason is ApprovalReason.UNKNOWN_APPROVAL


# --- 4. expiry ----------------------------------------------------------------


def test_an_expired_approval_cannot_execute() -> None:
    """Expiry is evaluated at execution time, so a lapsed record cannot run even
    if no sweeper has visited it."""
    svc = service(ttl_seconds=60.0)
    approval_id = pending(svc)
    svc.decide(approval_id, approved=True, approver=HUMAN)

    later = datetime.now(UTC) + timedelta(seconds=120)
    decision = svc.authorise_execution(
        approval_id, tool_name="propose_change_request", payload=PAYLOAD, now=later
    )
    assert decision.permitted is False
    assert decision.status is ApprovalStatus.EXPIRED
    assert decision.reason is ApprovalReason.TIME_EXPIRED


def test_an_expired_request_cannot_be_decided() -> None:
    svc = service(ttl_seconds=60.0)
    approval_id = pending(svc)
    later = datetime.now(UTC) + timedelta(seconds=120)
    with pytest.raises(ApprovalError) as caught:
        svc.decide(approval_id, approved=True, approver=HUMAN, now=later)
    assert caught.value.reason is ApprovalReason.TIME_EXPIRED


def test_an_unexpired_approval_still_works() -> None:
    svc = service(ttl_seconds=3600.0)
    approval_id = pending(svc)
    svc.decide(approval_id, approved=True, approver=HUMAN)
    soon = datetime.now(UTC) + timedelta(seconds=60)
    assert svc.authorise_execution(
        approval_id, tool_name="propose_change_request", payload=PAYLOAD, now=soon
    ).permitted


# --- 5. argument mutation invalidates approval --------------------------------


def test_altering_arguments_after_approval_invalidates_it() -> None:
    """Approving one action and executing another is the substitution this
    check exists to prevent."""
    svc = service()
    approval_id = pending(svc)
    svc.decide(approval_id, approved=True, approver=HUMAN)

    altered = ProposeChangeInput(
        title="Raise PROD capacity", rationale="Evaluation runs are throttled."
    )
    decision = svc.authorise_execution(
        approval_id, tool_name="propose_change_request", payload=altered
    )
    assert decision.permitted is False
    assert decision.reason is ApprovalReason.ARGUMENTS_CHANGED


def test_a_single_changed_character_invalidates_the_approval() -> None:
    svc = service()
    approval_id = pending(svc)
    svc.decide(approval_id, approved=True, approver=HUMAN)

    nudged = ProposeChangeInput(
        title="Raise sandbox capacity.", rationale="Evaluation runs are throttled."
    )
    assert not svc.authorise_execution(
        approval_id, tool_name="propose_change_request", payload=nudged
    ).permitted


def test_swapping_the_tool_invalidates_the_approval() -> None:
    svc = service()
    approval_id = pending(svc)
    svc.decide(approval_id, approved=True, approver=HUMAN)
    decision = svc.authorise_execution(approval_id, tool_name="some_other_tool", payload=PAYLOAD)
    assert decision.permitted is False
    assert decision.reason is ApprovalReason.ARGUMENTS_CHANGED


# --- 6. self-approval is impossible -------------------------------------------


def test_the_requester_cannot_approve_its_own_request() -> None:
    """A system that can approve its own requests has no approval step, only a delay."""
    svc = service()
    approval_id = pending(svc, requested_by="agt-abc123")
    with pytest.raises(ApprovalError) as caught:
        svc.decide(approval_id, approved=True, approver="agt-abc123")
    assert caught.value.reason is ApprovalReason.SELF_APPROVAL_REJECTED


@pytest.mark.parametrize(
    "principal",
    ["agent:planner", "model:gpt-4-1-mini", "system:scheduler", "agt-xyz", "req-123", ""],
)
def test_a_machine_principal_can_never_approve(principal: str) -> None:
    svc = service()
    approval_id = pending(svc)
    with pytest.raises(ApprovalError) as caught:
        svc.decide(approval_id, approved=True, approver=principal)
    assert caught.value.reason is ApprovalReason.SELF_APPROVAL_REJECTED


def test_a_human_principal_is_recognised() -> None:
    assert is_machine_principal("agent:x") is True
    assert is_machine_principal(HUMAN) is False


def test_an_approved_record_names_who_approved_it() -> None:
    svc = service()
    approval_id = pending(svc)
    decided = svc.decide(approval_id, approved=True, approver=HUMAN)
    assert decided.decided_by == HUMAN
    assert decided.decided_at
    assert decided.reason is ApprovalReason.GRANTED_BY_HUMAN


# --- 7. duplicate approved action still blocked -------------------------------


def test_an_approved_action_still_cannot_execute_twice() -> None:
    """Approval authorises; Phase 18.2 idempotency still prevents duplication."""
    svc = service()
    approval_id = pending(svc)
    svc.decide(approval_id, approved=True, approver=HUMAN)
    assert svc.authorise_execution(
        approval_id, tool_name="propose_change_request", payload=PAYLOAD
    ).permitted

    tool = RecordingProposal()
    executor = ToolExecutor(
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.0), sleeper=lambda _: None
    )
    first = executor.execute(definition_for(tool), PAYLOAD, approved=True)
    second = executor.execute(definition_for(tool), PAYLOAD, approved=True)

    assert first.status is ExecutionStatus.SUCCEEDED
    assert second.status is ExecutionStatus.DUPLICATE_BLOCKED
    assert tool.performed == ["Raise sandbox capacity"]


def test_a_decision_cannot_be_made_twice() -> None:
    svc = service()
    approval_id = pending(svc)
    svc.decide(approval_id, approved=True, approver=HUMAN)
    with pytest.raises(ApprovalError) as caught:
        svc.decide(approval_id, approved=False, approver=HUMAN)
    assert caught.value.reason is ApprovalReason.ALREADY_DECIDED


def test_a_cancelled_request_is_terminal() -> None:
    svc = service()
    approval_id = pending(svc)
    svc.cancel(approval_id)
    assert not svc.authorise_execution(
        approval_id, tool_name="propose_change_request", payload=PAYLOAD
    ).permitted


# --- 8. read-only path unaffected ----------------------------------------------


def test_read_only_tools_remain_automatic() -> None:
    from tests.agent_fakes import search

    svc = service()
    turn = agent_service(search(), approvals=svc).run(AgentRequest(question=QUESTION))

    assert turn.response.outcome is AgentOutcomeKind.ANSWERED
    assert turn.response.approval_id is None
    assert svc.store.list_pending() == []


# --- API surface ----------------------------------------------------------------


def test_the_approval_api_lists_inspects_and_decides() -> None:
    app = create_app(
        FakeGenerationProvider(),
        agent_decider=FakeAgentDecisionProvider(use_tool("propose_change_request", PROPOSAL_ARGS)),
    )
    with TestClient(app) as client:
        turn = client.post("/v1/agent", json={"question": QUESTION}).json()
        approval_id = turn["approval_id"]
        assert turn["outcome"] == "approval_required"

        listed = client.get("/v1/approvals").json()["pending"]
        assert [item["approval_id"] for item in listed] == [approval_id]

        fetched = client.get(f"/v1/approvals/{approval_id}").json()
        assert fetched["effective_status"] == "pending"

        decided = client.post(
            f"/v1/approvals/{approval_id}/decision",
            json={"approved": True, "approver": HUMAN},
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "approved"
        assert client.get("/v1/approvals").json()["pending"] == []


def test_the_approval_api_refuses_a_machine_approver() -> None:
    app = create_app(
        FakeGenerationProvider(),
        agent_decider=FakeAgentDecisionProvider(use_tool("propose_change_request", PROPOSAL_ARGS)),
    )
    with TestClient(app) as client:
        approval_id = client.post("/v1/agent", json={"question": QUESTION}).json()["approval_id"]
        response = client.post(
            f"/v1/approvals/{approval_id}/decision",
            json={"approved": True, "approver": "agent:self"},
        )
        assert response.status_code == 409
        assert response.json()["reason"] == ApprovalReason.SELF_APPROVAL_REJECTED.value


def test_the_approval_decision_body_is_closed() -> None:
    app = create_app(FakeGenerationProvider(), agent_decider=FakeAgentDecisionProvider())
    with TestClient(app) as client:
        response = client.post(
            "/v1/approvals/apr-x/decision",
            json={"approved": True, "approver": HUMAN, "bypass_policy": True},
        )
        assert response.status_code == 422


def test_an_unknown_approval_is_a_404() -> None:
    app = create_app(FakeGenerationProvider(), agent_decider=FakeAgentDecisionProvider())
    with TestClient(app) as client:
        assert client.get("/v1/approvals/apr-nope").status_code == 404
