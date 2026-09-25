"""The Phase 18.5 audit trail: completeness, ordering, redaction, isolation.

Every test here runs offline against the real agent. Nothing is asserted about a
trajectory that was hand-built unless the test is specifically about `verify()`
detecting a malformed one.
"""

from __future__ import annotations

import json

import pytest

from platform_engineering_assistant.agent.approval import ApprovalService
from platform_engineering_assistant.agent.domain import (
    AgentOutcomeKind,
    AgentRequest,
    PolicyDecision,
    ToolExecutionStatus,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.trajectory import (
    AgentTrajectory,
    CompositeTrajectorySink,
    InMemoryTrajectorySink,
    JsonLinesTrajectorySink,
    TrajectoryDefectKind,
    TrajectoryEvent,
    TrajectoryEventKind,
    TrajectoryRecorder,
)
from platform_engineering_assistant.agent.workflow_engine import WorkflowEngine
from tests.agent_fakes import agent_service, no_tool, propose_change, refuse, search, use_tool

QUESTION = "How is Terraform state separated between environments?"


def run(service, question: str = QUESTION):  # type: ignore[no-untyped-def]
    return service.run(AgentRequest(question=question))


def kinds(trajectory: AgentTrajectory) -> list[TrajectoryEventKind]:
    return [event.kind for event in trajectory.events]


# --- 1. the trajectory covers the whole path --------------------------------


def test_a_no_tool_turn_records_request_decision_policy_and_outcome() -> None:
    turn = run(agent_service(no_tool()))
    assert turn.trajectory is not None
    assert kinds(turn.trajectory) == [
        TrajectoryEventKind.REQUEST_RECEIVED,
        TrajectoryEventKind.MODEL_DECISION,
        TrajectoryEventKind.POLICY_VERDICT,
        TrajectoryEventKind.OUTCOME,
    ]


def test_a_tool_using_turn_records_the_call_and_its_result() -> None:
    turn = run(agent_service(search()))
    assert turn.trajectory is not None
    assert kinds(turn.trajectory) == [
        TrajectoryEventKind.REQUEST_RECEIVED,
        TrajectoryEventKind.MODEL_DECISION,
        TrajectoryEventKind.POLICY_VERDICT,
        TrajectoryEventKind.TOOL_CALL,
        TrajectoryEventKind.TOOL_RESULT,
        TrajectoryEventKind.OUTCOME,
    ]


def test_the_recorded_call_and_result_share_the_executors_call_id() -> None:
    """One id across the audit trail and the execution outcome, or an auditor
    has to guess which record belongs to which action."""
    turn = run(agent_service(search()))
    assert turn.trajectory is not None
    call = turn.trajectory.of_kind(TrajectoryEventKind.TOOL_CALL)[0]
    result = turn.trajectory.of_kind(TrajectoryEventKind.TOOL_RESULT)[0]
    assert call.tool_call_id == result.tool_call_id
    assert call.tool_call_id == turn.telemetry.tool_call_id


def test_the_call_is_recorded_before_the_result() -> None:
    """A call that started and never returned must still appear."""
    turn = run(agent_service(search()))
    assert turn.trajectory is not None
    call = turn.trajectory.of_kind(TrajectoryEventKind.TOOL_CALL)[0]
    result = turn.trajectory.of_kind(TrajectoryEventKind.TOOL_RESULT)[0]
    assert call.sequence < result.sequence


def test_an_approval_requirement_records_the_request_and_executes_nothing() -> None:
    turn = run(agent_service(propose_change()))
    assert turn.response.outcome is AgentOutcomeKind.APPROVAL_REQUIRED
    assert turn.trajectory is not None
    assert TrajectoryEventKind.APPROVAL_REQUESTED in kinds(turn.trajectory)
    assert TrajectoryEventKind.TOOL_CALL not in kinds(turn.trajectory)

    requested = turn.trajectory.of_kind(TrajectoryEventKind.APPROVAL_REQUESTED)[0]
    assert requested.approval_id == turn.response.approval_id
    assert requested.tool_risk_level is ToolRiskLevel.STATE_CHANGING
    assert len(requested.argument_fingerprint or "") == 64


def test_a_denial_records_the_policy_verdict_and_its_reason() -> None:
    turn = run(agent_service(use_tool("delete_everything")))
    assert turn.trajectory is not None
    verdict = turn.trajectory.of_kind(TrajectoryEventKind.POLICY_VERDICT)[0]
    assert verdict.policy_decision is PolicyDecision.DENY
    assert verdict.denial_reason is not None
    assert turn.trajectory.outcome is AgentOutcomeKind.DENIED


def test_a_refusal_records_its_reason_on_the_outcome() -> None:
    turn = run(agent_service(refuse()))
    assert turn.trajectory is not None
    outcome = turn.trajectory.of_kind(TrajectoryEventKind.OUTCOME)[0]
    assert outcome.outcome is AgentOutcomeKind.REFUSED
    assert outcome.refusal_reason is not None


# --- 2. the model's risk claim is recorded, never acted on -------------------


def test_a_false_risk_claim_is_recorded_on_the_policy_verdict() -> None:
    """The claim is observable across turns; policy still read the registry."""
    turn = run(
        agent_service(
            use_tool(
                "propose_change_request",
                {"title": "Raise capacity", "rationale": "Sandbox is throttling."},
                claimed_risk=ToolRiskLevel.READ_ONLY,
            )
        )
    )
    assert turn.trajectory is not None
    verdict = turn.trajectory.of_kind(TrajectoryEventKind.POLICY_VERDICT)[0]
    assert verdict.claimed_risk_level is ToolRiskLevel.READ_ONLY
    assert verdict.tool_risk_level is ToolRiskLevel.STATE_CHANGING
    assert verdict.risk_claim_mismatch is True
    assert verdict.policy_decision is PolicyDecision.REQUIRE_APPROVAL


# --- 3. provenance and cost travel with the record ---------------------------


def test_the_trajectory_carries_the_versions_that_produced_it() -> None:
    turn = run(agent_service(no_tool()))
    assert turn.trajectory is not None
    assert turn.trajectory.agent_prompt_version == "agent_decision_v1"
    assert turn.trajectory.prompt_version
    assert turn.trajectory.retrieval_config_version
    assert turn.trajectory.corpus_version >= 1


def test_the_decision_event_carries_its_own_latency_and_the_outcome_the_total() -> None:
    turn = run(agent_service(search()))
    assert turn.trajectory is not None
    decision = turn.trajectory.of_kind(TrajectoryEventKind.MODEL_DECISION)[0]
    outcome = turn.trajectory.of_kind(TrajectoryEventKind.OUTCOME)[0]
    assert decision.duration_ms is not None
    assert outcome.duration_ms is not None
    assert outcome.elapsed_ms >= decision.elapsed_ms


# --- 4. redaction is structural ---------------------------------------------

FORBIDDEN_FIELD_NAMES = {
    "question",
    "answer",
    "prompt",
    "system_prompt",
    "text",
    "content",
    "arguments",
    "tool_arguments",
    "evidence",
    "context",
    "chunk_text",
    "reasoning",
    "chain_of_thought",
    "scratchpad",
    "rationale",
    "explanation",
    "summary",
    "credential",
    "secret",
    "api_key",
}


def test_no_trajectory_model_declares_a_field_for_content() -> None:
    for model in (TrajectoryEvent, AgentTrajectory):
        declared = set(model.model_fields)
        assert not declared & FORBIDDEN_FIELD_NAMES, f"{model.__name__} declares content fields"


def test_the_recorded_events_never_contain_the_question_text() -> None:
    """The strongest form of the check: search the serialised record for it."""
    marker = "PhraseThatOnlyAppearsInTheQuestion"
    sink = InMemoryTrajectorySink()
    service = agent_service(search(), trajectory_sink=sink)
    service.run(AgentRequest(question=f"What is {marker} in this platform?"))

    serialised = json.dumps(
        [event.as_dict() for event in sink.events_for(sink.trajectory_ids()[0])]
    )
    assert marker not in serialised
    assert marker.lower() not in serialised.lower()


def test_the_recorded_events_never_contain_a_tool_argument_value() -> None:
    marker = "UniqueQueryTokenHere"
    sink = InMemoryTrajectorySink()
    service = agent_service(search(query=marker), trajectory_sink=sink)
    service.run(AgentRequest(question=QUESTION))

    serialised = json.dumps(
        [event.as_dict() for event in sink.events_for(sink.trajectory_ids()[0])]
    )
    assert marker not in serialised
    # The count is recorded; the value is not.
    decision = [
        e
        for e in sink.events_for(sink.trajectory_ids()[0])
        if e.kind is TrajectoryEventKind.MODEL_DECISION
    ][0]
    assert decision.argument_count == 1


def test_an_event_rejects_an_undeclared_field() -> None:
    with pytest.raises(ValueError):
        TrajectoryEvent(
            event_id="ev-1",
            trajectory_id="agt-1",
            sequence=1,
            kind=TrajectoryEventKind.OUTCOME,
            occurred_at="2026-09-02T00:00:00+00:00",
            elapsed_ms=0.0,
            question="smuggled",  # type: ignore[call-arg]
        )


# --- 5. observability can never break the product ---------------------------


class ExplodingSink:
    """A sink that fails every time, as a broken backend would."""

    def __init__(self) -> None:
        self.attempts = 0

    def emit(self, event: TrajectoryEvent) -> None:
        self.attempts += 1
        raise RuntimeError("the telemetry backend is down")


def test_a_failing_sink_does_not_fail_the_turn() -> None:
    sink = ExplodingSink()
    turn = run(agent_service(search(), trajectory_sink=sink))
    assert turn.response.outcome is AgentOutcomeKind.ANSWERED
    assert sink.attempts > 0


def test_a_failing_sink_is_counted_rather_than_hidden() -> None:
    turn = run(agent_service(search(), trajectory_sink=ExplodingSink()))
    assert turn.trajectory is not None
    assert turn.trajectory.sink_failures == len(turn.trajectory.events)


def test_one_failing_sink_does_not_silence_the_others() -> None:
    good = InMemoryTrajectorySink()
    composite = CompositeTrajectorySink([ExplodingSink(), good])
    turn = run(agent_service(search(), trajectory_sink=composite))
    assert turn.response.outcome is AgentOutcomeKind.ANSWERED
    assert good.events_for(turn.response.request_id)
    assert composite.failures > 0


# --- 6. the sinks themselves -------------------------------------------------


def test_the_memory_sink_is_bounded_and_evicts_the_oldest() -> None:
    sink = InMemoryTrajectorySink(capacity=2)
    for index in range(3):
        TrajectoryRecorder(f"agt-{index}", sink).record(TrajectoryEventKind.REQUEST_RECEIVED)
    assert sink.trajectory_ids() == ("agt-1", "agt-2")
    assert sink.events_for("agt-0") == ()


def test_the_memory_sink_rejects_a_meaningless_capacity() -> None:
    with pytest.raises(ValueError):
        InMemoryTrajectorySink(capacity=0)


def test_the_jsonl_sink_appends_one_readable_line_per_event(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "nested" / "trajectory.jsonl"
    turn = run(agent_service(search(), trajectory_sink=JsonLinesTrajectorySink(path)))
    lines = path.read_text().strip().splitlines()
    assert turn.trajectory is not None
    assert len(lines) == len(turn.trajectory.events)
    assert json.loads(lines[0])["kind"] == TrajectoryEventKind.REQUEST_RECEIVED.value


def test_the_jsonl_sink_appends_rather_than_rewrites(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An audit trail that can be rewritten in place is not one."""
    path = tmp_path / "trajectory.jsonl"
    sink = JsonLinesTrajectorySink(path)
    first = run(agent_service(no_tool(), trajectory_sink=sink))
    before = len(path.read_text().strip().splitlines())
    run(agent_service(no_tool(), trajectory_sink=sink))
    assert first.trajectory is not None
    assert len(path.read_text().strip().splitlines()) == before * 2


# --- 7. verify(): the ordering invariants ------------------------------------


def event(kind: TrajectoryEventKind, sequence: int, **fields: object) -> TrajectoryEvent:
    return TrajectoryEvent(
        event_id=f"ev-{sequence}",
        trajectory_id="agt-test",
        sequence=sequence,
        kind=kind,
        occurred_at="2026-09-02T00:00:00+00:00",
        elapsed_ms=float(sequence),
        **fields,  # type: ignore[arg-type]
    )


def test_a_real_turn_is_well_formed() -> None:
    for decision in (no_tool(), search(), refuse(), propose_change()):
        turn = run(agent_service(decision))
        assert turn.trajectory is not None
        assert turn.trajectory.verify() == (), f"{decision.kind} produced defects"


def test_an_empty_trajectory_is_a_defect() -> None:
    defects = AgentTrajectory(trajectory_id="agt-1").verify()
    assert defects[0].kind is TrajectoryDefectKind.MISSING_REQUEST


def test_a_tool_call_with_no_policy_verdict_is_a_defect() -> None:
    """THE invariant: nothing reaches a tool without passing the policy layer."""
    trajectory = AgentTrajectory(
        trajectory_id="agt-1",
        events=(
            event(TrajectoryEventKind.REQUEST_RECEIVED, 1),
            event(
                TrajectoryEventKind.TOOL_CALL,
                2,
                tool_name="search_platform_docs",
                tool_risk_level=ToolRiskLevel.READ_ONLY,
                tool_call_id="tc-1",
            ),
            event(TrajectoryEventKind.TOOL_RESULT, 3, tool_call_id="tc-1"),
            event(TrajectoryEventKind.OUTCOME, 4, outcome=AgentOutcomeKind.ANSWERED),
        ),
    )
    assert TrajectoryDefectKind.TOOL_CALL_WITHOUT_POLICY in {d.kind for d in trajectory.verify()}


def test_a_state_changing_call_without_a_matching_approval_is_a_defect() -> None:
    trajectory = AgentTrajectory(
        trajectory_id="agt-1",
        events=(
            event(TrajectoryEventKind.REQUEST_RECEIVED, 1),
            event(
                TrajectoryEventKind.POLICY_VERDICT,
                2,
                policy_decision=PolicyDecision.REQUIRE_APPROVAL,
                tool_name="propose_change_request",
            ),
            event(
                TrajectoryEventKind.TOOL_CALL,
                3,
                tool_name="propose_change_request",
                tool_risk_level=ToolRiskLevel.STATE_CHANGING,
                tool_call_id="tc-1",
                argument_fingerprint="a" * 64,
            ),
            event(TrajectoryEventKind.TOOL_RESULT, 4, tool_call_id="tc-1"),
            event(TrajectoryEventKind.OUTCOME, 5, outcome=AgentOutcomeKind.ANSWERED),
        ),
    )
    assert TrajectoryDefectKind.TOOL_CALL_WITHOUT_APPROVAL in {d.kind for d in trajectory.verify()}


def test_an_approval_for_different_arguments_does_not_authorise_the_call() -> None:
    """Approving one action and executing another is the substitution the
    fingerprint comparison exists to catch."""
    trajectory = AgentTrajectory(
        trajectory_id="agt-1",
        events=(
            event(TrajectoryEventKind.REQUEST_RECEIVED, 1),
            event(
                TrajectoryEventKind.POLICY_VERDICT,
                2,
                policy_decision=PolicyDecision.REQUIRE_APPROVAL,
                tool_name="propose_change_request",
            ),
            event(
                TrajectoryEventKind.APPROVAL_DECIDED,
                3,
                approval_status="approved",
                argument_fingerprint="a" * 64,
            ),
            event(
                TrajectoryEventKind.TOOL_CALL,
                4,
                tool_name="propose_change_request",
                tool_risk_level=ToolRiskLevel.STATE_CHANGING,
                tool_call_id="tc-1",
                argument_fingerprint="b" * 64,
            ),
            event(TrajectoryEventKind.TOOL_RESULT, 5, tool_call_id="tc-1"),
            event(TrajectoryEventKind.OUTCOME, 6, outcome=AgentOutcomeKind.ANSWERED),
        ),
    )
    assert TrajectoryDefectKind.TOOL_CALL_WITHOUT_APPROVAL in {d.kind for d in trajectory.verify()}


def test_a_call_with_no_result_is_a_defect() -> None:
    trajectory = AgentTrajectory(
        trajectory_id="agt-1",
        events=(
            event(TrajectoryEventKind.REQUEST_RECEIVED, 1),
            event(
                TrajectoryEventKind.POLICY_VERDICT,
                2,
                policy_decision=PolicyDecision.ALLOW,
                tool_name="search_platform_docs",
            ),
            event(
                TrajectoryEventKind.TOOL_CALL,
                3,
                tool_name="search_platform_docs",
                tool_risk_level=ToolRiskLevel.READ_ONLY,
                tool_call_id="tc-1",
            ),
            event(TrajectoryEventKind.OUTCOME, 4, outcome=AgentOutcomeKind.FAILED),
        ),
    )
    assert TrajectoryDefectKind.TOOL_CALL_WITHOUT_RESULT in {d.kind for d in trajectory.verify()}


def test_an_iteration_beyond_the_ceiling_is_a_defect() -> None:
    trajectory = AgentTrajectory(
        trajectory_id="agt-1",
        events=(
            event(TrajectoryEventKind.REQUEST_RECEIVED, 1),
            event(
                TrajectoryEventKind.POLICY_VERDICT,
                2,
                policy_decision=PolicyDecision.ALLOW,
                tool_name="search_platform_docs",
            ),
            event(
                TrajectoryEventKind.TOOL_CALL,
                3,
                tool_name="search_platform_docs",
                tool_risk_level=ToolRiskLevel.READ_ONLY,
                tool_call_id="tc-1",
                iteration=3,
            ),
            event(TrajectoryEventKind.TOOL_RESULT, 4, tool_call_id="tc-1"),
            event(TrajectoryEventKind.OUTCOME, 5, outcome=AgentOutcomeKind.ANSWERED),
        ),
    )
    assert TrajectoryDefectKind.ITERATION_LIMIT_EXCEEDED in {d.kind for d in trajectory.verify()}


def test_a_consequential_event_after_the_outcome_is_a_defect() -> None:
    trajectory = AgentTrajectory(
        trajectory_id="agt-1",
        events=(
            event(TrajectoryEventKind.REQUEST_RECEIVED, 1),
            event(TrajectoryEventKind.OUTCOME, 2, outcome=AgentOutcomeKind.REFUSED),
            event(
                TrajectoryEventKind.POLICY_VERDICT,
                3,
                policy_decision=PolicyDecision.ALLOW,
                tool_name="search_platform_docs",
            ),
        ),
    )
    assert TrajectoryDefectKind.CONSEQUENCE_AFTER_OUTCOME in {d.kind for d in trajectory.verify()}


def test_a_state_transition_after_the_outcome_is_not_a_defect() -> None:
    """Persisting the terminal state genuinely is the last thing that happens."""
    trajectory = AgentTrajectory(
        trajectory_id="agt-1",
        events=(
            event(TrajectoryEventKind.REQUEST_RECEIVED, 1),
            event(TrajectoryEventKind.OUTCOME, 2, outcome=AgentOutcomeKind.REFUSED),
            event(
                TrajectoryEventKind.STATE_TRANSITION, 3, from_state="answer", to_state="completed"
            ),
        ),
    )
    assert trajectory.verify() == ()


def test_out_of_order_sequence_numbers_are_a_defect() -> None:
    trajectory = AgentTrajectory(
        trajectory_id="agt-1",
        events=(
            event(TrajectoryEventKind.REQUEST_RECEIVED, 1),
            event(TrajectoryEventKind.OUTCOME, 7, outcome=AgentOutcomeKind.REFUSED),
        ),
    )
    assert TrajectoryDefectKind.OUT_OF_ORDER in {d.kind for d in trajectory.verify()}


# --- 8. workflow state transitions and resumption ---------------------------


def test_workflow_state_transitions_are_recorded() -> None:
    engine = WorkflowEngine(agent_service(no_tool()))
    result = engine.start(AgentRequest(question=QUESTION))
    assert result.trajectory is not None
    transitions = result.trajectory.of_kind(TrajectoryEventKind.STATE_TRANSITION)
    assert [(t.from_state, t.to_state) for t in transitions] == [
        ("request", "decide"),
        ("decide", "policy"),
        ("policy", "answer"),
        ("answer", "completed"),
    ]


def test_a_started_workflow_produces_one_trajectory_not_two() -> None:
    engine = WorkflowEngine(agent_service(search()))
    result = engine.start(AgentRequest(question=QUESTION))
    assert result.trajectory is not None
    assert result.turn is not None
    assert result.trajectory.trajectory_id == result.turn.response.request_id
    assert result.trajectory.workflow_id == result.record.workflow_id
    assert result.trajectory.verify() == ()


def test_a_resumed_workflow_records_the_approval_the_call_and_the_result() -> None:
    approvals = ApprovalService()
    engine = WorkflowEngine(agent_service(propose_change(), approvals=approvals))
    started = engine.start(AgentRequest(question="Please raise the sandbox capacity."))
    assert started.waiting_for_approval

    approvals.decide(started.record.approval_id or "", approved=True, approver="alice@example.com")
    resumed = engine.resume(started.record.workflow_id)

    assert resumed.trajectory is not None
    recorded = kinds(resumed.trajectory)
    assert TrajectoryEventKind.APPROVAL_DECIDED in recorded
    assert TrajectoryEventKind.TOOL_CALL in recorded
    assert TrajectoryEventKind.TOOL_RESULT in recorded
    assert resumed.trajectory.workflow_id == started.record.workflow_id
    # The approval carried the authority across the interruption, so the call is
    # authorised even though policy ran in the earlier turn.
    assert resumed.trajectory.verify() == ()


def test_a_resumed_execution_is_linked_to_the_workflow_not_the_original_request() -> None:
    approvals = ApprovalService()
    engine = WorkflowEngine(agent_service(propose_change(), approvals=approvals))
    started = engine.start(AgentRequest(question="Please raise the sandbox capacity."))
    approvals.decide(started.record.approval_id or "", approved=True, approver="alice@example.com")
    resumed = engine.resume(started.record.workflow_id)

    assert resumed.trajectory is not None
    assert started.trajectory is not None
    assert resumed.trajectory.trajectory_id != started.trajectory.trajectory_id
    assert resumed.trajectory.workflow_id == started.trajectory.workflow_id


def test_a_refused_approval_is_recorded_as_a_denial_with_no_tool_call() -> None:
    approvals = ApprovalService()
    engine = WorkflowEngine(agent_service(propose_change(), approvals=approvals))
    started = engine.start(AgentRequest(question="Please raise the sandbox capacity."))
    approvals.decide(started.record.approval_id or "", approved=False, approver="alice@example.com")
    resumed = engine.resume(started.record.workflow_id)

    assert resumed.trajectory is not None
    assert TrajectoryEventKind.TOOL_CALL not in kinds(resumed.trajectory)
    assert resumed.trajectory.outcome is AgentOutcomeKind.DENIED


# --- 9. the human decision reaches the trail whoever recorded it -------------


def test_an_approval_decision_is_recorded_on_the_originating_turns_trajectory() -> None:
    sink = InMemoryTrajectorySink()
    approvals = ApprovalService(trajectory_sink=sink)
    service = agent_service(propose_change(), approvals=approvals, trajectory_sink=sink)
    turn = run(service, "Please raise the sandbox capacity.")

    approvals.decide(turn.response.approval_id or "", approved=True, approver="alice@example.com")

    events = sink.events_for(turn.response.request_id)
    decided = [e for e in events if e.kind is TrajectoryEventKind.APPROVAL_DECIDED]
    assert len(decided) == 1
    assert decided[0].approver == "alice@example.com"
    assert decided[0].approval_status == "approved"


def test_a_self_approval_is_refused_and_records_no_decision() -> None:
    sink = InMemoryTrajectorySink()
    approvals = ApprovalService(trajectory_sink=sink)
    service = agent_service(propose_change(), approvals=approvals, trajectory_sink=sink)
    turn = run(service, "Please raise the sandbox capacity.")

    from platform_engineering_assistant.agent.approval import ApprovalError

    with pytest.raises(ApprovalError):
        approvals.decide(turn.response.approval_id or "", approved=True, approver="agent:self")

    events = sink.events_for(turn.response.request_id)
    assert not [e for e in events if e.kind is TrajectoryEventKind.APPROVAL_DECIDED]


# --- 10. the OTel mapping ----------------------------------------------------


def test_events_map_onto_the_gen_ai_semantic_conventions() -> None:
    turn = run(agent_service(search()))
    assert turn.trajectory is not None
    call = turn.trajectory.of_kind(TrajectoryEventKind.TOOL_CALL)[0]
    attributes = call.as_otel_attributes()
    assert attributes["gen_ai.operation.name"] == "invoke_agent"
    assert attributes["gen_ai.tool.name"] == "search_platform_docs"
    assert attributes["gen_ai.tool.call.id"] == call.tool_call_id
    assert attributes["agent.tool.risk_level"] == ToolRiskLevel.READ_ONLY.value


def test_the_otel_mapping_carries_no_content_either() -> None:
    marker = "AnotherUniqueQueryToken"
    sink = InMemoryTrajectorySink()
    service = agent_service(search(query=marker), trajectory_sink=sink)
    service.run(AgentRequest(question=f"Tell me about {marker}."))
    rendered = json.dumps(
        [
            {key: str(value) for key, value in event.as_otel_attributes().items()}
            for event in sink.events_for(sink.trajectory_ids()[0])
        ]
    )
    assert marker not in rendered


def test_every_exported_attribute_is_a_scalar() -> None:
    """A nested structure is where content hides. Attributes stay flat."""
    turn = run(agent_service(search()))
    assert turn.trajectory is not None
    for event in turn.trajectory.events:
        for value in event.as_otel_attributes().values():
            assert isinstance(value, (str, int, float, bool)), value


# --- 11. execution failures are visible -------------------------------------


def test_a_tool_failure_is_recorded_with_its_category_and_attempts() -> None:
    """A call that failed is still a call. It must appear, with why it failed."""
    from platform_engineering_assistant.agent.registry import ToolDefinition, ToolRegistry
    from platform_engineering_assistant.agent.tools import (
        SearchDocsInput,
        SearchDocsOutput,
        SearchPlatformDocsTool,
    )

    class TimingOutSearch(SearchPlatformDocsTool):
        def run(self, payload: SearchDocsInput) -> SearchDocsOutput:
            raise TimeoutError("the upstream index did not respond")

    from platform_engineering_assistant.retrieval.index import build_index
    from tests.agent_fakes import DEFAULT_CHUNKS
    from tests.generation_fakes import CONFIG

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="search_platform_docs",
                description="Search the approved documentation.",
                risk=ToolRiskLevel.READ_ONLY,
                tool=TimingOutSearch(build_index(DEFAULT_CHUNKS, CONFIG)),  # type: ignore[arg-type]
            )
        ]
    )
    service = agent_service(search(), registry=registry)
    turn = service.run(AgentRequest(question=QUESTION))

    assert turn.trajectory is not None
    results = turn.trajectory.of_kind(TrajectoryEventKind.TOOL_RESULT)
    assert results, "a failed call must still record a result"
    assert results[0].execution_status is ToolExecutionStatus.FAILED
    assert results[0].failure_category is not None
    assert results[0].attempts >= 1
    assert turn.trajectory.verify() == ()
