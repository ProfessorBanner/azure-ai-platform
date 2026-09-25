"""Resuming an approved proposal. The one path that may execute a state change.

    resume(workflow_id)
      -> approval re-checked AT EXECUTION TIME  (Phase 18 ApprovalService)
      -> already performed?  stop, do not repeat
      -> execute exactly once                   (Phase 18 ToolExecutor)
      -> terminal state

Everything consequential is a product decision. This module contributes the
ordering and the refusal to ask twice.

WHY THE OUTCOME IS NOT ASSUMED TO BE SUCCESS
--------------------------------------------
Phase 18's own resume path records a FAILED execution as `answered` and marks
the action performed — a defect found during Phase 18 live acceptance and left
open there. This lab does not copy it: the execution status is read, a failure
moves the workflow to FAILED, and the call is recorded as completed only when it
actually completed. `propose_change_request` performs no external mutation, so
this is a difference in bookkeeping honesty, not in blast radius.
"""

from __future__ import annotations

from dataclasses import dataclass

from foundry_agent_lab._product import ensure_product_importable
from foundry_agent_lab.governance import GovernedToolGateway
from foundry_agent_lab.workflow import InMemoryWorkflowStore, WorkflowState

ensure_product_importable()

from platform_engineering_assistant.agent.approval import ApprovalStatus  # noqa: E402
from platform_engineering_assistant.agent.execution import ExecutionStatus  # noqa: E402
from platform_engineering_assistant.agent.tools import ProposeChangeInput  # noqa: E402


@dataclass(frozen=True, slots=True)
class ResumeResult:
    """What a resume attempt did. `executed` is the only consequential bit."""

    workflow_id: str
    state: WorkflowState
    executed: bool
    tool_call_id: str | None = None
    reason: str = ""
    already_performed: bool = False


class ApprovedActionRunner:
    """Executes an approved proposal at most once."""

    def __init__(self, gateway: GovernedToolGateway, store: InMemoryWorkflowStore) -> None:
        self._gateway = gateway
        self._store = store

    def resume(self, workflow_id: str) -> ResumeResult:
        """Attempt to run the approved action. Refuses far more often than it runs.

        Raises:
            KeyError: when the workflow is unknown. An unknown id is a caller
                error, not a governance outcome, and must not look like a
                refusal.
        """
        record = self._store.get(workflow_id)
        if record is None:
            raise KeyError(f"Unknown workflow: {workflow_id}")

        if record.is_terminal:
            # Terminal is terminal. A denied workflow does not become approvable
            # by being resumed, and a completed one does not run again.
            return ResumeResult(
                workflow_id=workflow_id,
                state=record.state,
                executed=False,
                reason="workflow is already terminal",
                already_performed=bool(record.completed_call_ids),
            )

        if record.completed_call_ids:
            return ResumeResult(
                workflow_id=workflow_id,
                state=record.state,
                executed=False,
                reason="the action was already performed",
                already_performed=True,
            )

        definition = self._gateway.registry.get(record.tool_name)
        if definition is None:
            moved = record.moved_to(WorkflowState.FAILED, "tool is no longer registered")
            self._store.put(moved)
            return ResumeResult(workflow_id, moved.state, False, reason=moved.detail)

        payload = ProposeChangeInput.model_validate(record.arguments)

        # THE check. Re-validates tool name, argument fingerprint and expiry
        # against the approval AS IT STANDS NOW, not as it stood when granted.
        decision = self._gateway.approvals.authorise_execution(
            record.approval_id, tool_name=record.tool_name, payload=payload
        )
        if not decision.permitted:
            state = (
                WorkflowState.DENIED
                if decision.status
                in (ApprovalStatus.DENIED, ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED)
                else record.state
            )
            moved = record.moved_to(state, str(decision.reason))
            self._store.put(moved)
            return ResumeResult(
                workflow_id=workflow_id,
                state=moved.state,
                executed=False,
                reason=str(decision.reason),
            )

        outcome = self._gateway.executor.execute(definition, payload, approved=True)
        if outcome.status is not ExecutionStatus.SUCCEEDED:
            # Recorded as failed, and NOT recorded as performed. The idempotency
            # store still holds the claim, so whether it is safe to repeat stays
            # a human's decision rather than something inferred here.
            moved = record.moved_to(WorkflowState.FAILED, str(outcome.failure_category))
            self._store.put(moved)
            return ResumeResult(
                workflow_id=workflow_id,
                state=moved.state,
                executed=False,
                tool_call_id=outcome.tool_call_id,
                reason=str(outcome.failure_category),
            )

        moved = record.with_completed_call(outcome.tool_call_id).moved_to(WorkflowState.COMPLETED)
        self._store.put(moved)
        return ResumeResult(
            workflow_id=workflow_id,
            state=moved.state,
            executed=True,
            tool_call_id=outcome.tool_call_id,
        )


__all__ = ["ApprovedActionRunner", "ResumeResult"]
