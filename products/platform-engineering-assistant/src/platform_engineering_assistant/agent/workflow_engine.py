"""Driving the agent through persisted, resumable workflow states.

    start(question)
      REQUEST -> DECIDE -> POLICY -> { ANSWER -> COMPLETED
                                     | DENIED
                                     | EXECUTE -> ANSWER -> COMPLETED
                                     | WAITING_APPROVAL  <- persisted, returns }

    resume(workflow_id)
      WAITING_APPROVAL -> { EXECUTE -> COMPLETED   when the approval is granted
                          | DENIED                 when it is refused or lapsed }

WHAT RESUMPTION CHANGES, AND WHAT IT MUST NOT
---------------------------------------------
Resumption introduces exactly one new hazard: an action performed before the
interruption being performed again after it. Two independent mechanisms prevent
that, and both are needed because they fail differently.

  1. The workflow records the tool_call_id of every completed action, so a
     resume can see that the work is done without consulting the tool.
  2. The Phase 18.2 idempotency store still holds the claim on the action's key,
     so even a workflow record that was lost cannot produce a second action.

The engine never bypasses policy or approval. A resumed workflow re-checks the
approval at the moment of execution — an approval that expired while the process
was down is not honoured just because it was granted before the interruption.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from platform_engineering_assistant.agent.approval import ApprovalService, ApprovalStatus
from platform_engineering_assistant.agent.domain import (
    AgentOutcomeKind,
    AgentRequest,
    AgentResponse,
    ToolExecutionStatus,
)
from platform_engineering_assistant.agent.execution import idempotency_key_for
from platform_engineering_assistant.agent.orchestrator import AgentService, AgentTurn
from platform_engineering_assistant.agent.trajectory import (
    AgentTrajectory,
    TrajectoryEventKind,
    TrajectoryRecorder,
    TrajectorySink,
)
from platform_engineering_assistant.agent.workflow import (
    InMemoryWorkflowStore,
    ToolProposal,
    WorkflowRecord,
    WorkflowState,
    WorkflowStore,
    new_workflow,
)

# How an agent outcome maps onto the workflow state it leaves behind.
_OUTCOME_STATES: dict[AgentOutcomeKind, WorkflowState] = {
    AgentOutcomeKind.ANSWERED: WorkflowState.ANSWER,
    AgentOutcomeKind.REFUSED: WorkflowState.ANSWER,
    AgentOutcomeKind.DENIED: WorkflowState.DENIED,
    AgentOutcomeKind.FAILED: WorkflowState.FAILED,
    AgentOutcomeKind.APPROVAL_REQUIRED: WorkflowState.WAITING_APPROVAL,
}


@dataclass(frozen=True, slots=True)
class WorkflowTurn:
    """An agent turn plus the durable record it left behind."""

    record: WorkflowRecord
    response: AgentResponse | None = None
    turn: AgentTurn | None = None
    # Phase 18.5. For `start` this is the turn's own trajectory with the
    # workflow's state transitions appended; for `resume` it is the trajectory
    # of the resumed execution, linked to the same workflow_id.
    trajectory: AgentTrajectory | None = None

    @property
    def waiting_for_approval(self) -> bool:
        return self.record.state is WorkflowState.WAITING_APPROVAL


class WorkflowEngine:
    """Runs agent turns and persists their state so they can be resumed."""

    def __init__(
        self,
        agent: AgentService,
        store: WorkflowStore | None = None,
        approvals: ApprovalService | None = None,
        trajectory_sink: TrajectorySink | None = None,
    ) -> None:
        self._agent = agent
        self._store = store or InMemoryWorkflowStore()
        # Defaults to the agent's own approval service, so a decision recorded
        # through the API is the same decision this engine reads.
        self._approvals = approvals or agent.approvals
        # Defaults to the agent's sink, so a resumed execution is recorded to
        # the same place as the turn that was interrupted. A workflow whose
        # audit trail lands somewhere else is a workflow nobody can follow.
        self._trajectory_sink = (
            trajectory_sink if trajectory_sink is not None else agent.trajectory_sink
        )

    @property
    def store(self) -> WorkflowStore:
        return self._store

    def start(self, request: AgentRequest, request_id: str | None = None) -> WorkflowTurn:
        """Run a turn from the beginning, persisting each state it passes through."""
        turn = self._agent.run(request, request_id=request_id)
        response = turn.response
        # Continue the turn's own record rather than opening a second one. A
        # workflow that produced two trajectories would leave a reader to guess
        # which half of a turn they were looking at.
        recorder = turn.recorder or TrajectoryRecorder(
            trajectory_id=response.request_id, sink=self._trajectory_sink
        )

        record = new_workflow(
            request_id=response.request_id,
            question=request.question,
            agent_prompt_version=response.agent_prompt_version,
            prompt_version=response.prompt_version,
            retrieval_config_version=response.retrieval_config_version,
            corpus_version=response.corpus_version,
        )
        self._store.put(record)
        recorder.bind_workflow(record.workflow_id)

        # The turn already happened in one call, so the intermediate states are
        # recorded rather than driven. They are still written, because an audit
        # asks which states a workflow passed through, not how fast.
        record = self._advance(record, WorkflowState.DECIDE, recorder)
        record = self._advance(record, WorkflowState.POLICY, recorder)

        proposal = self._proposal_from(response, turn.proposed_arguments)
        target = _OUTCOME_STATES[response.outcome]

        if target is WorkflowState.WAITING_APPROVAL:
            record = self._advance(
                record,
                WorkflowState.WAITING_APPROVAL,
                recorder,
                proposal=proposal,
                approval_id=response.approval_id,
                outcome=response.outcome,
                iterations=response.tool_iterations,
            )
            self._store.put(record)
            return self._turn(record, recorder, response=response, turn=turn)

        if target is WorkflowState.ANSWER:
            if response.tool_iterations:
                record = self._advance(record, WorkflowState.EXECUTE, recorder, proposal=proposal)
            record = self._advance(
                record,
                WorkflowState.ANSWER,
                recorder,
                outcome=response.outcome,
                iterations=response.tool_iterations,
            )
            record = self._advance(record, WorkflowState.COMPLETED, recorder)
        else:
            record = self._advance(
                record, target, recorder, outcome=response.outcome, proposal=proposal
            )

        self._store.put(record)
        return self._turn(record, recorder, response=response, turn=turn)

    def resume(self, workflow_id: str) -> WorkflowTurn:
        """Continue a workflow that stopped for approval.

        Raises:
            KeyError: when the workflow is unknown.
        """
        record = self._store.get(workflow_id)
        if record is None:
            raise KeyError(f"Unknown workflow: {workflow_id}")

        # A resume is its own turn and gets its own trajectory, linked to the
        # workflow. Appending to the interrupted turn's record would place
        # events under a correlation id whose request finished long ago,
        # possibly in another process.
        recorder = TrajectoryRecorder(
            trajectory_id=f"rsm-{uuid.uuid4().hex[:16]}",
            sink=self._trajectory_sink,
            workflow_id=record.workflow_id,
        )
        recorder.record(TrajectoryEventKind.REQUEST_RECEIVED, question_chars=len(record.question))

        if record.is_terminal:
            # Terminal is terminal. A denied workflow does not become approvable
            # by being resumed, and a completed one does not run again.
            recorder.record(
                TrajectoryEventKind.OUTCOME,
                outcome=record.outcome,
                from_state=record.state.value,
                to_state=record.state.value,
            )
            return self._turn(record, recorder)

        if record.state is not WorkflowState.WAITING_APPROVAL:
            recorder.record(
                TrajectoryEventKind.OUTCOME,
                outcome=record.outcome,
                from_state=record.state.value,
                to_state=record.state.value,
            )
            return self._turn(record, recorder)

        approval_id = record.approval_id
        approval = self._approvals.store.get(approval_id) if approval_id else None
        status = approval.effective_status() if approval else None

        # The approval as it stands AT THE MOMENT OF EXECUTION, which is the
        # only moment whose answer matters. An approval that lapsed while the
        # process was down is recorded here as expired, not as granted.
        recorder.record(
            TrajectoryEventKind.APPROVAL_DECIDED,
            approval_id=approval_id,
            approval_status=status.value if status is not None else None,
            approval_reason=(
                approval.reason.value if approval and approval.reason is not None else None
            ),
            approver=approval.decided_by if approval else None,
            argument_fingerprint=(approval.action.argument_fingerprint if approval else None),
            tool_name=record.proposal.tool_name if record.proposal else None,
        )

        if status is not ApprovalStatus.APPROVED:
            # Not approved, refused, cancelled, or lapsed while the process was
            # down. All are the same answer: this does not execute.
            record = self._advance(
                record, WorkflowState.DENIED, recorder, outcome=AgentOutcomeKind.DENIED
            )
            self._store.put(record)
            recorder.record(TrajectoryEventKind.OUTCOME, outcome=AgentOutcomeKind.DENIED)
            return self._turn(record, recorder)

        if record.completed_tool_call_ids:
            # The action already ran before the interruption. Resuming must not
            # repeat it; the workflow simply completes.
            record = self._advance(record, WorkflowState.EXECUTE, recorder)
            record = self._advance(
                record, WorkflowState.ANSWER, recorder, outcome=AgentOutcomeKind.ANSWERED
            )
            record = self._advance(record, WorkflowState.COMPLETED, recorder)
            self._store.put(record)
            recorder.record(TrajectoryEventKind.OUTCOME, outcome=AgentOutcomeKind.ANSWERED)
            return self._turn(record, recorder)

        record = self._advance(record, WorkflowState.EXECUTE, recorder)
        self._store.put(record)

        outcome = self._execute_approved(record, recorder)
        record = record.with_completed_call(outcome)
        record = self._advance(
            record, WorkflowState.ANSWER, recorder, outcome=AgentOutcomeKind.ANSWERED
        )
        record = self._advance(record, WorkflowState.COMPLETED, recorder)
        self._store.put(record)
        recorder.record(TrajectoryEventKind.OUTCOME, outcome=AgentOutcomeKind.ANSWERED)
        return self._turn(record, recorder)

    def _execute_approved(self, record: WorkflowRecord, recorder: TrajectoryRecorder) -> str:
        """Execute the approved action exactly once.

        Re-validates the approval against the persisted proposal at the moment
        of execution, so an approval that lapsed during the interruption is not
        honoured on the strength of having once been granted.
        """
        from platform_engineering_assistant.agent.tools import ProposeChangeInput

        proposal = record.proposal
        assert proposal is not None  # WAITING_APPROVAL always records one

        definition = self._agent.registry.get(proposal.tool_name)
        if definition is None:
            raise KeyError(f"Unknown tool on resume: {proposal.tool_name}")

        payload = ProposeChangeInput.model_validate(proposal.arguments)
        decision = self._approvals.authorise_execution(
            record.approval_id or "", tool_name=proposal.tool_name, payload=payload
        )
        if not decision.permitted:
            raise PermissionError(f"Approval no longer permits execution: {decision.reason}")

        tool_call_id = f"tc-{uuid.uuid4().hex[:16]}"
        recorder.record(
            TrajectoryEventKind.TOOL_CALL,
            tool_name=definition.name,
            tool_risk_level=definition.risk,
            tool_call_id=tool_call_id,
            argument_fingerprint=idempotency_key_for(definition.name, payload),
            argument_count=len(proposal.arguments),
            iteration=record.iterations + 1,
        )
        result = self._agent.executor.execute(
            definition, payload, approved=True, tool_call_id=tool_call_id
        )
        recorder.record(
            TrajectoryEventKind.TOOL_RESULT,
            tool_name=definition.name,
            tool_risk_level=definition.risk,
            tool_call_id=result.tool_call_id,
            argument_fingerprint=result.idempotency_key,
            execution_status=(
                ToolExecutionStatus.SUCCEEDED if result.succeeded else ToolExecutionStatus.FAILED
            ),
            failure_category=(
                result.failure_category.value if result.failure_category is not None else None
            ),
            attempts=result.attempts,
            duration_ms=result.total_latency_ms,
        )
        return result.tool_call_id

    def _advance(
        self,
        record: WorkflowRecord,
        state: WorkflowState,
        recorder: TrajectoryRecorder,
        **updates: object,
    ) -> WorkflowRecord:
        """Move the record and record the move. One place, so they cannot diverge.

        The transition is performed FIRST: an illegal move raises, and an audit
        trail must not claim a state change that the state machine refused.
        """
        moved = record.moved_to(state, **updates)
        recorder.record(
            TrajectoryEventKind.STATE_TRANSITION,
            from_state=record.state.value,
            to_state=state.value,
        )
        return moved

    def _turn(
        self,
        record: WorkflowRecord,
        recorder: TrajectoryRecorder,
        *,
        response: AgentResponse | None = None,
        turn: AgentTurn | None = None,
    ) -> WorkflowTurn:
        return WorkflowTurn(
            record=record,
            response=response,
            turn=turn,
            trajectory=recorder.build(
                agent_prompt_version=record.agent_prompt_version,
                prompt_version=record.prompt_version,
                retrieval_config_version=record.retrieval_config_version,
                corpus_version=record.corpus_version,
            ),
        )

    @staticmethod
    def _proposal_from(response: AgentResponse, arguments: dict[str, str]) -> ToolProposal | None:
        """Persist the EXACT arguments, so a resume authorises the same action.

        These are server-side durable state, not response content: resuming an
        approved action requires the arguments the approval fingerprinted, and
        an approximation would fail that comparison — correctly, but uselessly.
        """
        if not response.selected_tool or not response.tool_risk_level:
            return None
        return ToolProposal(
            tool_name=response.selected_tool,
            arguments=dict(arguments),
            risk_level=response.tool_risk_level.value,
        )


__all__ = ["WorkflowEngine", "WorkflowTurn"]
