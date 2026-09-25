"""Phase 18.4: durable workflow state, resumption and replay safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from platform_engineering_assistant.agent.approval import ApprovalService, InMemoryApprovalStore
from platform_engineering_assistant.agent.domain import (
    AgentDecision,
    AgentOutcomeKind,
    AgentRequest,
)
from platform_engineering_assistant.agent.workflow import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InMemoryWorkflowStore,
    InvalidTransitionError,
    JsonFileWorkflowStore,
    WorkflowState,
    WorkflowStore,
    describe_graph,
    new_workflow,
)
from platform_engineering_assistant.agent.workflow_engine import WorkflowEngine
from tests.agent_fakes import agent_service, no_tool, search, use_tool

QUESTION = "Should sandbox capacity be raised for evaluation runs?"
PROPOSAL_ARGS = {"title": "Raise sandbox capacity", "rationale": "Evaluation runs are throttled."}
HUMAN = "bruce.banner@example.com"


def engine_for(
    decision: AgentDecision,
    store: WorkflowStore | None = None,
    approvals: ApprovalService | None = None,
) -> WorkflowEngine:
    approvals = approvals or ApprovalService(InMemoryApprovalStore())
    agent = agent_service(decision, approvals=approvals)
    return WorkflowEngine(agent, store=store or InMemoryWorkflowStore(), approvals=approvals)


# --- the transition table ------------------------------------------------------


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_every_state_appears_in_the_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(WorkflowState)


def test_an_invalid_transition_is_rejected_rather_than_performed() -> None:
    record = new_workflow(request_id="agt-1", question=QUESTION)
    with pytest.raises(InvalidTransitionError):
        record.moved_to(WorkflowState.COMPLETED)  # REQUEST cannot jump to COMPLETED


def test_a_waiting_workflow_cannot_return_to_policy() -> None:
    """Re-authorising an action a human has already ruled on would make the
    ruling advisory."""
    assert WorkflowState.POLICY not in ALLOWED_TRANSITIONS[WorkflowState.WAITING_APPROVAL]


def test_the_graph_can_be_described() -> None:
    rendered = describe_graph()
    assert "waiting_approval" in rendered
    assert "(terminal)" in rendered


# --- 1. pause at approval -------------------------------------------------------


def test_a_state_changing_proposal_pauses_the_workflow() -> None:
    engine = engine_for(use_tool("propose_change_request", PROPOSAL_ARGS))
    turn = engine.start(AgentRequest(question=QUESTION))

    assert turn.waiting_for_approval
    assert turn.record.state is WorkflowState.WAITING_APPROVAL
    assert turn.record.approval_id
    assert turn.record.proposal is not None
    assert turn.record.proposal.arguments == PROPOSAL_ARGS


def test_a_paused_workflow_is_persisted_and_listed() -> None:
    store = InMemoryWorkflowStore()
    engine = engine_for(use_tool("propose_change_request", PROPOSAL_ARGS), store=store)
    turn = engine.start(AgentRequest(question=QUESTION))

    assert store.get(turn.record.workflow_id) is not None
    assert [r.workflow_id for r in store.list_waiting()] == [turn.record.workflow_id]


# --- 2. resume after approval ----------------------------------------------------


def test_an_approved_workflow_resumes_and_completes() -> None:
    approvals = ApprovalService(InMemoryApprovalStore())
    engine = engine_for(use_tool("propose_change_request", PROPOSAL_ARGS), approvals=approvals)
    turn = engine.start(AgentRequest(question=QUESTION))

    approvals.decide(turn.record.approval_id or "", approved=True, approver=HUMAN)
    resumed = engine.resume(turn.record.workflow_id)

    assert resumed.record.state is WorkflowState.COMPLETED
    assert resumed.record.completed_tool_call_ids


def test_a_denied_approval_makes_the_workflow_terminal() -> None:
    approvals = ApprovalService(InMemoryApprovalStore())
    engine = engine_for(use_tool("propose_change_request", PROPOSAL_ARGS), approvals=approvals)
    turn = engine.start(AgentRequest(question=QUESTION))

    approvals.decide(turn.record.approval_id or "", approved=False, approver=HUMAN)
    resumed = engine.resume(turn.record.workflow_id)

    assert resumed.record.state is WorkflowState.DENIED
    assert resumed.record.is_terminal


def test_resuming_without_a_decision_denies_rather_than_executes() -> None:
    """Only an explicit approved state permits execution."""
    engine = engine_for(use_tool("propose_change_request", PROPOSAL_ARGS))
    turn = engine.start(AgentRequest(question=QUESTION))
    resumed = engine.resume(turn.record.workflow_id)
    assert resumed.record.state is WorkflowState.DENIED


def test_a_denied_workflow_stays_terminal_when_resumed_again() -> None:
    approvals = ApprovalService(InMemoryApprovalStore())
    engine = engine_for(use_tool("propose_change_request", PROPOSAL_ARGS), approvals=approvals)
    turn = engine.start(AgentRequest(question=QUESTION))
    approvals.decide(turn.record.approval_id or "", approved=False, approver=HUMAN)

    first = engine.resume(turn.record.workflow_id)
    second = engine.resume(turn.record.workflow_id)
    assert first.record.state is second.record.state is WorkflowState.DENIED


# --- 3. restart and resume across processes ---------------------------------------


def test_a_workflow_survives_a_process_restart(tmp_path: Path) -> None:
    """The actual Phase 18.4 requirement: a NEW engine, a new store instance,
    reading state written by the one that stopped."""
    directory = tmp_path / "workflows"
    approvals = ApprovalService(InMemoryApprovalStore())

    first_engine = engine_for(
        use_tool("propose_change_request", PROPOSAL_ARGS),
        store=JsonFileWorkflowStore(directory),
        approvals=approvals,
    )
    started = first_engine.start(AgentRequest(question=QUESTION))
    workflow_id = started.record.workflow_id

    approvals.decide(started.record.approval_id or "", approved=True, approver=HUMAN)

    # A different engine and a freshly constructed store: nothing in memory is shared.
    second_engine = engine_for(
        use_tool("propose_change_request", PROPOSAL_ARGS),
        store=JsonFileWorkflowStore(directory),
        approvals=approvals,
    )
    resumed = second_engine.resume(workflow_id)

    assert resumed.record.workflow_id == workflow_id
    assert resumed.record.state is WorkflowState.COMPLETED


def test_the_file_store_round_trips_a_record(tmp_path: Path) -> None:
    store = JsonFileWorkflowStore(tmp_path)
    record = new_workflow(request_id="agt-1", question=QUESTION)
    store.put(record)

    reloaded = JsonFileWorkflowStore(tmp_path).get(record.workflow_id)
    assert reloaded is not None
    assert reloaded.workflow_id == record.workflow_id
    assert reloaded.question == QUESTION


def test_an_unknown_workflow_reads_as_absent(tmp_path: Path) -> None:
    assert JsonFileWorkflowStore(tmp_path).get("wf-nope") is None


def test_a_corrupt_record_does_not_crash_the_store(tmp_path: Path) -> None:
    (tmp_path / "wf-broken.json").write_text("{not json")
    store = JsonFileWorkflowStore(tmp_path)
    assert store.get("wf-broken") is None
    assert store.list_waiting() == []


def test_resuming_an_unknown_workflow_raises() -> None:
    with pytest.raises(KeyError):
        engine_for(no_tool()).resume("wf-does-not-exist")


# --- 4. completed actions are never re-executed -------------------------------------


def test_a_completed_action_is_not_executed_again_on_resume() -> None:
    """The one new hazard resumption introduces, closed by the workflow record."""
    approvals = ApprovalService(InMemoryApprovalStore())
    engine = engine_for(use_tool("propose_change_request", PROPOSAL_ARGS), approvals=approvals)
    turn = engine.start(AgentRequest(question=QUESTION))
    approvals.decide(turn.record.approval_id or "", approved=True, approver=HUMAN)

    first = engine.resume(turn.record.workflow_id)
    calls_after_first = first.record.completed_tool_call_ids

    second = engine.resume(turn.record.workflow_id)
    assert second.record.state is WorkflowState.COMPLETED
    assert second.record.completed_tool_call_ids == calls_after_first


def test_idempotency_remains_the_second_line_of_defence() -> None:
    """Even a lost workflow record cannot produce a second consequential action."""
    approvals = ApprovalService(InMemoryApprovalStore())
    agent = agent_service(use_tool("propose_change_request", PROPOSAL_ARGS), approvals=approvals)
    engine = WorkflowEngine(agent, store=InMemoryWorkflowStore(), approvals=approvals)

    turn = engine.start(AgentRequest(question=QUESTION))
    approvals.decide(turn.record.approval_id or "", approved=True, approver=HUMAN)
    engine.resume(turn.record.workflow_id)

    # Execute the same action directly, as a lost-record replay would.
    from platform_engineering_assistant.agent.execution import ExecutionStatus
    from platform_engineering_assistant.agent.tools import ProposeChangeInput

    definition = agent.registry.get("propose_change_request")
    assert definition is not None
    replay = agent.executor.execute(
        definition, ProposeChangeInput.model_validate(PROPOSAL_ARGS), approved=True
    )
    assert replay.status is ExecutionStatus.DUPLICATE_BLOCKED


def test_a_record_tracks_the_calls_it_performed() -> None:
    record = new_workflow(request_id="agt-1", question=QUESTION)
    assert record.has_performed("tc-1") is False
    updated = record.with_completed_call("tc-1")
    assert updated.has_performed("tc-1") is True


# --- 5. bounded iterations survive resume ---------------------------------------------


def test_max_iterations_is_persisted_and_bounded() -> None:
    engine = engine_for(search())
    turn = engine.start(AgentRequest(question=QUESTION))
    assert turn.record.iterations <= 2


def test_the_iteration_ceiling_is_enforced_by_the_record_schema() -> None:
    from pydantic import ValidationError

    record = new_workflow(request_id="agt-1", question=QUESTION)
    with pytest.raises(ValidationError):
        record.model_copy(update={"iterations": 99}).model_validate(
            record.model_copy(update={"iterations": 99}).model_dump()
        )


def test_iterations_survive_a_restart(tmp_path: Path) -> None:
    store = JsonFileWorkflowStore(tmp_path)
    engine = engine_for(search(), store=store)
    turn = engine.start(AgentRequest(question=QUESTION))

    reloaded = JsonFileWorkflowStore(tmp_path).get(turn.record.workflow_id)
    assert reloaded is not None
    assert reloaded.iterations == turn.record.iterations


# --- non-approval paths -----------------------------------------------------------------


def test_a_no_tool_answer_completes_immediately() -> None:
    turn = engine_for(no_tool()).start(AgentRequest(question=QUESTION))
    assert turn.record.state is WorkflowState.COMPLETED
    assert turn.record.outcome is AgentOutcomeKind.ANSWERED
    assert not turn.waiting_for_approval


def test_a_read_only_tool_turn_records_its_execute_state() -> None:
    turn = engine_for(search()).start(AgentRequest(question=QUESTION))
    assert turn.record.state is WorkflowState.COMPLETED
    assert any("execute" in entry for entry in turn.record.history)


def test_a_denied_proposal_is_terminal_without_approval() -> None:
    turn = engine_for(use_tool("not_a_tool")).start(AgentRequest(question=QUESTION))
    assert turn.record.state is WorkflowState.DENIED
    assert turn.record.is_terminal


def test_provenance_travels_with_the_workflow() -> None:
    turn = engine_for(no_tool()).start(AgentRequest(question=QUESTION))
    assert turn.record.agent_prompt_version == "agent_decision_v1"
    assert turn.record.corpus_version == 1


def test_the_record_carries_no_answer_text() -> None:
    """A resumed workflow re-derives content; it persists DECISION state."""
    import json

    turn = engine_for(no_tool()).start(AgentRequest(question=QUESTION))
    payload = json.loads(turn.record.model_dump_json())
    for forbidden in ("answer", "citations", "evidence", "context", "reasoning"):
        assert forbidden not in payload
