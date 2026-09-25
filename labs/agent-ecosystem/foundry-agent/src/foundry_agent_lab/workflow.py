"""Durable-enough workflow state for an approval that outlives one turn.

The lab owns the SEQUENCING; it owns none of the controls. Whether a resumed
action may run is decided entirely by the Phase 18 approval service, which
re-checks the tool name, the argument fingerprint and the expiry at the moment
of execution — not at the moment of approval. Duplicate suppression is the Phase
18 idempotency store. This module only remembers which workflow is waiting for
which approval, and refuses to ask twice.

TWO INDEPENDENT REPLAY DEFENCES, KEPT
-------------------------------------
Phase 18 uses two because they fail differently, and that reasoning does not
change when the orchestration is managed:

  1. the record remembers the tool_call_id of a completed action, so a resume
     can see the work is done without consulting the tool;
  2. the idempotency store still holds the claim on the action's key, so even a
     lost record cannot produce a second action.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum


class WorkflowState(StrEnum):
    """Where a governed proposal has got to."""

    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    DENIED = "denied"
    FAILED = "failed"


TERMINAL = frozenset({WorkflowState.COMPLETED, WorkflowState.DENIED, WorkflowState.FAILED})


@dataclass(frozen=True, slots=True)
class LabWorkflow:
    """One state-changing proposal awaiting, or past, a human decision."""

    workflow_id: str
    conversation_id: str
    turn_id: str
    tool_name: str
    arguments: dict[str, str]
    approval_id: str
    state: WorkflowState = WorkflowState.WAITING_APPROVAL
    completed_call_ids: tuple[str, ...] = ()
    detail: str = ""
    history: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def moved_to(self, state: WorkflowState, detail: str = "") -> LabWorkflow:
        return replace(
            self,
            state=state,
            detail=detail or self.detail,
            history=(*self.history, f"{self.state}->{state}"),
        )

    def with_completed_call(self, tool_call_id: str) -> LabWorkflow:
        return replace(self, completed_call_ids=(*self.completed_call_ids, tool_call_id))


class InMemoryWorkflowStore:
    """Process-local store. Honest about its limits, as Phase 18's is."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, LabWorkflow] = {}

    def put(self, record: LabWorkflow) -> None:
        with self._lock:
            self._records[record.workflow_id] = record

    def get(self, workflow_id: str) -> LabWorkflow | None:
        with self._lock:
            return self._records.get(workflow_id)

    def list_waiting(self) -> list[LabWorkflow]:
        with self._lock:
            records = list(self._records.values())
        return [r for r in records if r.state is WorkflowState.WAITING_APPROVAL]


def new_workflow(
    *,
    conversation_id: str,
    turn_id: str,
    tool_name: str,
    arguments: dict[str, str],
    approval_id: str,
) -> LabWorkflow:
    return LabWorkflow(
        workflow_id=f"wf-{uuid.uuid4().hex[:16]}",
        conversation_id=conversation_id,
        turn_id=turn_id,
        tool_name=tool_name,
        arguments=dict(arguments),
        approval_id=approval_id,
    )


__all__ = [
    "TERMINAL",
    "InMemoryWorkflowStore",
    "LabWorkflow",
    "WorkflowState",
    "new_workflow",
]
