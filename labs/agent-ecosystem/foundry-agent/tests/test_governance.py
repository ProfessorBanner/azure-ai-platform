"""19.1c: a state-changing proposal from Foundry meets the Phase 18 controls.

Every ruling asserted here is made by product code. The lab supplies the
adapter, so these tests are really asking one question: does routing a proposal
through managed orchestration weaken any control it would have met in the
application's own loop? It must not.
"""

from __future__ import annotations

import dataclasses

import pytest

from foundry_agent_lab._product import ensure_product_importable
from foundry_agent_lab.governance import GovernedOutcome, GovernedToolGateway
from foundry_agent_lab.governed_runtime import GovernedFoundryLoop
from foundry_agent_lab.protocol import ProposedCall
from foundry_agent_lab.resume import ApprovedActionRunner
from foundry_agent_lab.workflow import InMemoryWorkflowStore, WorkflowState
from tests.fakes import PROPOSAL, answering, governed_gateway, proposing, runtime

ensure_product_importable()

from platform_engineering_assistant.agent.approval import ApprovalError  # noqa: E402
from platform_engineering_assistant.agent.domain import ToolRiskLevel  # noqa: E402

HUMAN = "manual-local-human"


def propose(**overrides: str) -> ProposedCall:
    return ProposedCall("call-1", "propose_change_request", {**PROPOSAL, **overrides})


def governed(gateway: GovernedToolGateway, *responses):  # type: ignore[no-untyped-def]
    store = InMemoryWorkflowStore()
    loop = GovernedFoundryLoop(runtime(*responses), gateway, store)
    return loop, store


# --- risk is server-owned ----------------------------------------------------


def test_risk_comes_from_the_product_registry() -> None:
    gateway = governed_gateway()
    assert gateway.registry.risk_of("propose_change_request") is ToolRiskLevel.STATE_CHANGING
    assert gateway.registry.risk_of("search_platform_docs") is ToolRiskLevel.READ_ONLY


def test_a_compromised_model_cannot_declare_a_tool_read_only() -> None:
    """Risk manipulation, the Phase 18 invariant, over Foundry's transport.

    There is no field on a function call for risk, so the only way to assert one
    is to smuggle it in as an argument — which the tool's closed input model
    rejects before policy is even consulted.
    """
    result = governed_gateway().govern(propose(risk="read_only"), "agt-test")
    assert result.outcome is GovernedOutcome.DENIED
    assert result.denial_reason == "invalid_arguments"
    assert result.tool_call_id is None


def test_a_state_changing_tool_never_reaches_the_execution_path() -> None:
    gateway = governed_gateway()
    result = gateway.govern(propose(), "agt-test")
    assert result.outcome is GovernedOutcome.APPROVAL_REQUIRED
    assert result.risk == "state_changing"
    assert result.tool_call_id is None


def test_an_unknown_tool_is_denied_and_never_executed() -> None:
    result = governed_gateway().govern(
        ProposedCall("c", "run_terraform_apply", {"environment": "prod"}), "agt-test"
    )
    assert result.outcome is GovernedOutcome.DENIED
    assert result.denial_reason == "unknown_tool"


def test_a_read_only_tool_still_executes_normally() -> None:
    result = governed_gateway().govern(
        ProposedCall("c", "search_platform_docs", {"query": "terraform state separation"}),
        "agt-test",
    )
    assert result.executed
    assert result.risk == "read_only"
    assert "RETRIEVED EVIDENCE" in result.output


# --- the initial request stops, and binds the approval ----------------------


def test_the_first_request_returns_approval_required_without_executing() -> None:
    gateway = governed_gateway()
    loop, store = governed(gateway, proposing(propose()), answering("recorded for approval"))
    turn = loop.run("Please raise a change request for sandbox capacity.")

    assert turn.approval_required
    assert turn.executed_tools == ()
    assert len(turn.workflow_ids) == 1
    waiting = store.get(turn.workflow_ids[0])
    assert waiting is not None and waiting.state is WorkflowState.WAITING_APPROVAL


def test_the_approval_binds_the_exact_tool_and_argument_fingerprint() -> None:
    gateway = governed_gateway()
    result = gateway.govern(propose(), "agt-test")
    record = gateway.approvals.store.get(result.approval_id or "")

    assert record.action.tool_name == "propose_change_request"
    assert record.action.argument_fingerprint == result.argument_fingerprint
    assert len(record.action.argument_fingerprint) == 64
    assert record.status.value == "pending"


def test_foundry_is_told_the_action_stopped_rather_than_being_left_silent() -> None:
    result = governed_gateway().govern(propose(), "agt-test")
    assert result.output.startswith("APPROVAL_REQUIRED")


# --- approval bypass ---------------------------------------------------------


def test_an_undecided_approval_cannot_execute() -> None:
    """The bypass attempt: resume without any human having decided."""
    gateway = governed_gateway()
    loop, store = governed(gateway, proposing(propose()), answering())
    turn = loop.run("raise a change request")

    outcome = ApprovedActionRunner(gateway, store).resume(turn.workflow_ids[0])
    assert outcome.executed is False
    assert "not_approved" in outcome.reason


def test_a_denied_approval_cannot_execute() -> None:
    gateway = governed_gateway()
    loop, store = governed(gateway, proposing(propose()), answering())
    turn = loop.run("raise a change request")
    gateway.approvals.decide(turn.approval_ids[0], approved=False, approver=HUMAN)

    outcome = ApprovedActionRunner(gateway, store).resume(turn.workflow_ids[0])
    assert outcome.executed is False
    assert outcome.state is WorkflowState.DENIED


def test_an_expired_approval_cannot_execute() -> None:
    """Expiry is evaluated at EXECUTION time, not by a sweeper."""
    gateway = governed_gateway(ttl_seconds=0.0)
    loop, store = governed(gateway, proposing(propose()), answering())
    turn = loop.run("raise a change request")

    with pytest.raises(ApprovalError):
        gateway.approvals.decide(turn.approval_ids[0], approved=True, approver=HUMAN)

    outcome = ApprovedActionRunner(gateway, store).resume(turn.workflow_ids[0])
    assert outcome.executed is False


def test_arguments_changed_after_approval_cannot_execute() -> None:
    """Approve one action, execute another: the substitution the fingerprint catches."""
    gateway = governed_gateway()
    loop, store = governed(gateway, proposing(propose()), answering())
    turn = loop.run("raise a change request")
    gateway.approvals.decide(turn.approval_ids[0], approved=True, approver=HUMAN)

    record = store.get(turn.workflow_ids[0])
    mutated = dict(record.arguments)
    mutated["title"] = "Raise sandbox capacity to 3000"
    store.put(dataclasses.replace(record, arguments=mutated))

    outcome = ApprovedActionRunner(gateway, store).resume(turn.workflow_ids[0])
    assert outcome.executed is False
    assert "arguments_changed" in outcome.reason


# --- self-approval -----------------------------------------------------------


def test_the_agent_cannot_approve_its_own_request() -> None:
    """Two controls catch this, and the machine-principal one fires first.

    The turn id is `agt-...`, which is a machine prefix, so the request is
    refused as a machine principal before the requester comparison is reached.
    Either refusal is correct; the assertion is on the REASON class, not on
    which control happened to win.
    """
    gateway = governed_gateway()
    loop, _ = governed(gateway, proposing(propose()), answering())
    turn = loop.run("raise a change request")

    with pytest.raises(ApprovalError) as error:
        gateway.approvals.decide(turn.approval_ids[0], approved=True, approver=turn.turn_id)
    assert error.value.reason.value == "self_approval_rejected"


@pytest.mark.parametrize("principal", ["agent:foundry", "model:gpt-4-1-mini", "system:lab"])
def test_a_machine_principal_cannot_approve(principal: str) -> None:
    gateway = governed_gateway()
    loop, _ = governed(gateway, proposing(propose()), answering())
    turn = loop.run("raise a change request")

    with pytest.raises(ApprovalError):
        gateway.approvals.decide(turn.approval_ids[0], approved=True, approver=principal)


# --- the approved path executes exactly once --------------------------------


def approved_workflow() -> tuple[GovernedToolGateway, InMemoryWorkflowStore, str]:
    gateway = governed_gateway()
    loop, store = governed(gateway, proposing(propose()), answering())
    turn = loop.run("raise a change request")
    gateway.approvals.decide(turn.approval_ids[0], approved=True, approver=HUMAN)
    return gateway, store, turn.workflow_ids[0]


def test_an_approved_workflow_resumes_and_reaches_a_terminal_state() -> None:
    gateway, store, workflow_id = approved_workflow()
    outcome = ApprovedActionRunner(gateway, store).resume(workflow_id)

    record = store.get(workflow_id)
    assert record is not None and record.is_terminal
    # `propose_change_request.run` raises by design (the Phase 18 tripwire), so
    # the execution fails. This lab records that honestly rather than calling it
    # a success — see resume.py.
    assert outcome.executed is False
    assert outcome.state is WorkflowState.FAILED


def test_a_second_resume_cannot_execute_again() -> None:
    gateway, store, workflow_id = approved_workflow()
    runner = ApprovedActionRunner(gateway, store)
    runner.resume(workflow_id)
    second = runner.resume(workflow_id)

    assert second.executed is False
    assert "terminal" in second.reason


def test_the_idempotency_claim_survives_a_failed_action() -> None:
    """Whether a failed consequential action may repeat is a human's decision."""
    gateway, store, workflow_id = approved_workflow()
    ApprovedActionRunner(gateway, store).resume(workflow_id)

    from platform_engineering_assistant.agent.execution import idempotency_key_for
    from platform_engineering_assistant.agent.tools import ProposeChangeInput

    key = idempotency_key_for("propose_change_request", ProposeChangeInput.model_validate(PROPOSAL))
    assert gateway.executor.store.claim(key) is False, "the claim was released"


def test_an_unknown_workflow_raises_rather_than_looking_like_a_refusal() -> None:
    gateway = governed_gateway()
    with pytest.raises(KeyError):
        ApprovedActionRunner(gateway, InMemoryWorkflowStore()).resume("wf-nope")


# --- the schema sent to Foundry ---------------------------------------------


def test_the_schemas_sent_to_foundry_never_carry_risk() -> None:
    gateway = governed_gateway()
    loop, _ = governed(gateway, answering())
    rendered = repr(loop.tool_schemas())
    assert "propose_change_request" in rendered
    assert "state_changing" not in rendered
    assert "read_only" not in rendered


def test_serving_a_turn_never_writes_to_the_agent_definition() -> None:
    """Provisioning is a separate, deliberate step. A read path must not write."""
    gateway = governed_gateway()
    loop, _ = governed(gateway, answering("direct answer"))
    loop.run("q")
    assert loop._runtime.ensure_calls == []


def test_the_turn_records_the_provisioned_version_it_ran_against() -> None:
    gateway = governed_gateway()
    store = InMemoryWorkflowStore()
    loop = GovernedFoundryLoop(
        runtime(answering()), gateway, store, agent_name="a", agent_version="7"
    )
    turn = loop.run("q")
    assert turn.agent_name == "a"
    assert turn.agent_version == "7"


def test_the_instructions_tell_the_model_it_does_not_decide_safety() -> None:
    from foundry_agent_lab.governed_runtime import GOVERNED_INSTRUCTIONS

    lowered = GOVERNED_INSTRUCTIONS.lower()
    assert "you do not decide whether a tool is safe" in lowered
    assert "never performs the change" in lowered
