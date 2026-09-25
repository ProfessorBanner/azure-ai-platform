"""Durable agent workflow state: an explicit, persisted state machine.

WHY THIS IS HAND-WRITTEN AND NOT LANGGRAPH
------------------------------------------
The Phase 18.4 brief asked whether LangGraph earns its place for resumability,
approval waits, branching, retry state and interrupted workflows. It was
assessed against the real dependency metadata, and it does not — yet.

  * `langgraph` requires `langchain-core`, which requires **`langsmith`** as a
    hard, non-optional dependency. Adopting LangGraph therefore installs
    LangSmith, which Phase 18 explicitly deferred and which is a third-party
    data-egress decision, not a library choice.
  * The full transitive addition is thirteen or more packages into a product
    whose entire runtime dependency set is five.
  * Its durable checkpointers are SQLite or Postgres. Postgres would be new
    Azure infrastructure, which this phase forbids; SQLite is a file, which is
    what the store below already is.
  * The graph itself is six live states with ONE wait state and a hard ceiling
    of two tool iterations. The transition table fits on a screen and is
    enforced here as data.

What LangGraph would genuinely add — and what would justify revisiting it — is
concurrent fan-out, streaming intermediate state to a UI, and time-travel
debugging across long multi-step trajectories. None of those exist yet.

THE STATE MACHINE IS DATA, NOT CONTROL FLOW
-------------------------------------------
`ALLOWED_TRANSITIONS` is a table. Every move is checked against it and an
illegal move raises rather than being quietly performed. That is what makes
"can this workflow execute now?" answerable by inspection rather than by tracing
code paths — the property that matters once a workflow can be resumed by a
different process than the one that started it.

REPLAY SAFETY
-------------
A resumed workflow must never repeat an action it already performed. Completed
tool calls are recorded by id, and the Phase 18.2 idempotency store remains the
second line of defence. Two independent mechanisms, because a duplicated
consequential action is the failure that resumption makes newly possible.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from platform_engineering_assistant.agent.domain import MAX_TOOL_ITERATIONS, AgentOutcomeKind


class WorkflowState(StrEnum):
    """Where a workflow is. Persisted, and the basis of every resume decision."""

    REQUEST = "request"
    DECIDE = "decide"
    POLICY = "policy"
    EXECUTE = "execute"
    WAITING_APPROVAL = "waiting_approval"
    ANSWER = "answer"
    DENIED = "denied"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STATES = frozenset({WorkflowState.COMPLETED, WorkflowState.DENIED, WorkflowState.FAILED})

# The whole graph, as data. Read it top to bottom to know what can happen.
ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.REQUEST: frozenset({WorkflowState.DECIDE, WorkflowState.FAILED}),
    WorkflowState.DECIDE: frozenset({WorkflowState.POLICY, WorkflowState.FAILED}),
    WorkflowState.POLICY: frozenset(
        {
            WorkflowState.EXECUTE,
            WorkflowState.WAITING_APPROVAL,
            WorkflowState.ANSWER,
            WorkflowState.DENIED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.EXECUTE: frozenset(
        {WorkflowState.ANSWER, WorkflowState.DECIDE, WorkflowState.FAILED}
    ),
    # An approved workflow may execute; a denied one is terminal. There is
    # deliberately no edge back to POLICY: re-authorising an action a human has
    # already ruled on would make the ruling advisory.
    WorkflowState.WAITING_APPROVAL: frozenset(
        {WorkflowState.EXECUTE, WorkflowState.DENIED, WorkflowState.FAILED}
    ),
    WorkflowState.ANSWER: frozenset({WorkflowState.COMPLETED, WorkflowState.FAILED}),
    WorkflowState.COMPLETED: frozenset(),
    WorkflowState.DENIED: frozenset(),
    WorkflowState.FAILED: frozenset(),
}


class InvalidTransitionError(Exception):
    """An illegal move was attempted. Never performed quietly."""

    def __init__(self, current: WorkflowState, requested: WorkflowState) -> None:
        super().__init__(f"Cannot move from {current} to {requested}.")
        self.current = current
        self.requested = requested


class ToolProposal(BaseModel):
    """The persisted proposal, so a resume knows exactly what was authorised."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1)
    arguments: dict[str, str] = Field(default_factory=dict)
    risk_level: str = Field(min_length=1)


class WorkflowRecord(BaseModel):
    """One durable workflow. Everything needed to resume it, and nothing else.

    Carries no answer text, no evidence and no reasoning: a resumed workflow
    re-derives those from the corpus. What it persists is the DECISION state —
    which is what cannot be re-derived and what an audit actually needs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1)
    state: WorkflowState
    request_id: str = Field(min_length=1)
    question: str = Field(min_length=1)

    proposal: ToolProposal | None = None
    approval_id: str | None = None
    outcome: AgentOutcomeKind | None = None

    iterations: int = Field(default=0, ge=0, le=MAX_TOOL_ITERATIONS)
    completed_tool_call_ids: tuple[str, ...] = ()

    # Provenance travels with the workflow so a resumed run can be shown to have
    # used the same instructions and corpus as the run that started it.
    agent_prompt_version: str = ""
    prompt_version: str = ""
    retrieval_config_version: str = ""
    corpus_version: int = 0

    created_at: str
    updated_at: str
    history: tuple[str, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def can_move_to(self, state: WorkflowState) -> bool:
        return state in ALLOWED_TRANSITIONS[self.state]

    def moved_to(self, state: WorkflowState, **updates: object) -> WorkflowRecord:
        """Return the record advanced to `state`.

        Raises:
            InvalidTransitionError: when the move is not in the table.
        """
        if not self.can_move_to(state):
            raise InvalidTransitionError(self.state, state)
        now = datetime.now(UTC).isoformat()
        return self.model_copy(
            update={
                "state": state,
                "updated_at": now,
                "history": (*self.history, f"{now} {self.state}->{state}"),
                **updates,
            }
        )

    def with_completed_call(self, tool_call_id: str) -> WorkflowRecord:
        """Record a performed action so a replay cannot repeat it."""
        return self.model_copy(
            update={"completed_tool_call_ids": (*self.completed_tool_call_ids, tool_call_id)}
        )

    def has_performed(self, tool_call_id: str) -> bool:
        return tool_call_id in self.completed_tool_call_ids


class WorkflowStore(Protocol):
    """Persistence for workflow records."""

    def put(self, record: WorkflowRecord) -> None: ...

    def get(self, workflow_id: str) -> WorkflowRecord | None: ...

    def list_waiting(self) -> list[WorkflowRecord]: ...


class InMemoryWorkflowStore:
    """Process-local store, for tests and single-process use."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, WorkflowRecord] = {}

    def put(self, record: WorkflowRecord) -> None:
        with self._lock:
            self._records[record.workflow_id] = record

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        with self._lock:
            return self._records.get(workflow_id)

    def list_waiting(self) -> list[WorkflowRecord]:
        with self._lock:
            records = list(self._records.values())
        return [r for r in records if r.state is WorkflowState.WAITING_APPROVAL]


class JsonFileWorkflowStore:
    """Durable across restarts, using a directory of JSON files.

    Deliberately the simplest thing that survives a process restart, which is
    the actual Phase 18.4 requirement. No database, no new Azure resource, and
    no schema migration to own. One file per workflow keeps concurrent writes to
    different workflows from contending, and the record is small.

    Writes go to a temporary file and are then renamed, so a crash mid-write
    leaves the previous record intact rather than a truncated one.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, workflow_id: str) -> Path:
        # Workflow ids are server-generated hex, but the check is cheap and the
        # consequence of getting it wrong is a write outside the directory.
        safe = "".join(ch for ch in workflow_id if ch.isalnum() or ch in "-_")
        return self._directory / f"{safe}.json"

    def put(self, record: WorkflowRecord) -> None:
        path = self._path(record.workflow_id)
        temporary = path.with_suffix(".json.tmp")
        with self._lock:
            temporary.write_text(record.model_dump_json(indent=2))
            temporary.replace(path)

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        path = self._path(workflow_id)
        try:
            raw = path.read_text()
        except OSError:
            return None
        try:
            return WorkflowRecord.model_validate_json(raw)
        except ValueError:
            return None

    def list_waiting(self) -> list[WorkflowRecord]:
        records: list[WorkflowRecord] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                record = WorkflowRecord.model_validate_json(path.read_text())
            except (OSError, ValueError):
                continue
            if record.state is WorkflowState.WAITING_APPROVAL:
                records.append(record)
        return records


def new_workflow(
    *,
    request_id: str,
    question: str,
    agent_prompt_version: str = "",
    prompt_version: str = "",
    retrieval_config_version: str = "",
    corpus_version: int = 0,
) -> WorkflowRecord:
    """Create a workflow in its initial state."""
    now = datetime.now(UTC).isoformat()
    return WorkflowRecord(
        workflow_id=f"wf-{uuid.uuid4().hex[:16]}",
        state=WorkflowState.REQUEST,
        request_id=request_id,
        question=question,
        agent_prompt_version=agent_prompt_version,
        prompt_version=prompt_version,
        retrieval_config_version=retrieval_config_version,
        corpus_version=corpus_version,
        created_at=now,
        updated_at=now,
    )


def describe_graph() -> str:
    """The transition table, rendered. Used in documentation and tests."""
    lines = []
    for state in WorkflowState:
        targets = sorted(ALLOWED_TRANSITIONS[state])
        lines.append(f"{state}: {', '.join(targets) if targets else '(terminal)'}")
    return "\n".join(lines)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "InMemoryWorkflowStore",
    "InvalidTransitionError",
    "JsonFileWorkflowStore",
    "ToolProposal",
    "WorkflowRecord",
    "WorkflowState",
    "WorkflowStore",
    "describe_graph",
    "new_workflow",
]
