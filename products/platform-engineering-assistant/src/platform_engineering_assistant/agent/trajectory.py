"""The agent audit trail: one ordered, redacted, verifiable record per turn.

    request  ->  model decision  ->  policy verdict  ->  approval
             ->  tool call  ->  tool result  ->  outcome

Every arrow above is an EVENT, and the sequence of events is the trajectory. The
existing `AgentTelemetry` summarises a turn in one row, which answers "what
happened" but not "in what order, and what was true at each step". An audit of a
consequential action needs the second question answered — particularly once a
turn can be interrupted by an approval and resumed by a different process.

WHY EVENTS RATHER THAN A WIDER TELEMETRY ROW
-------------------------------------------
A row cannot express ordering, and ordering is where the safety properties live.
"No tool ran before policy authorised it" and "no state-changing tool ran before
a human approved it" are statements about SEQUENCE. Expressed as events they are
checkable by `AgentTrajectory.verify()` against a recorded run — offline, with no
model — which is what makes them evaluable in Phase 18.6 rather than merely
asserted in a unit test.

REDACTION IS STRUCTURAL, AS EVERYWHERE ELSE IN THIS PRODUCT
------------------------------------------------------------
There is no field for the question, the answer, a prompt, tool ARGUMENTS,
retrieved evidence or any reasoning. Not "these are stripped before writing" —
the fields do not exist, so there is no step for a future edit to forget. The
structural test in `tests/test_source_hygiene.py` fails if one is added.

Three things are recorded that look like content and are not:

  `argument_fingerprint`   The Phase 18.2 idempotency hash over the VALIDATED
                           arguments. It correlates "the same action" across a
                           proposal, an approval and an execution without
                           carrying what the action says. A hash of an argument
                           set is not a way back to the argument set.

  `approver`               An approval nobody signed cannot be audited, so the
                           deciding principal is recorded. This is the one
                           identity field, and it is present because
                           accountability is the point of the record.

  `claimed_risk_level`     The model's assertion about a tool's risk, which
                           policy ignores. Recorded so a systematic attempt to
                           misclassify tools is visible across turns rather than
                           only within one.

NO HIDDEN CHAIN-OF-THOUGHT
--------------------------
Deliberately absent, for the reason `agent/domain.py` gives: such text is
unvalidated model output that leaks prompt content, invites a plausible
narrative to be read as justification, and would have to be redacted from every
sink. It is not collected, so it cannot be exported.

OBSERVABILITY MUST NOT BE ABLE TO BREAK THE PRODUCT
---------------------------------------------------
A sink that raises is swallowed and counted. A logging backend, a file system or
a remote exporter failing is not a reason for a grounded answer to fail, and an
agent whose availability depends on its telemetry pipeline has traded a real
property for a reporting one.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from platform_engineering_assistant.agent.domain import (
    MAX_TOOL_ITERATIONS,
    AgentOutcomeKind,
    DecisionKind,
    DenialReason,
    PolicyDecision,
    ToolExecutionStatus,
    ToolRiskLevel,
)
from platform_engineering_assistant.domain import RefusalReason

# Identifier and label bounds. Every string field on an event is an identifier,
# an enum value or a hash; these caps stop a long value being smuggled through
# one of them and into a log.
MAX_IDENTIFIER_LENGTH = 200


class TrajectoryEventKind(StrEnum):
    """The steps a turn can pass through. The whole vocabulary, in order."""

    REQUEST_RECEIVED = "request_received"
    MODEL_DECISION = "model_decision"
    POLICY_VERDICT = "policy_verdict"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STATE_TRANSITION = "state_transition"
    OUTCOME = "outcome"


class TrajectoryDefectKind(StrEnum):
    """What `verify()` can find wrong with a recorded trajectory.

    Each one is a property that must hold of EVERY turn, so a defect here is a
    control failure rather than a quality signal. `TOOL_CALL_WITHOUT_POLICY` and
    `TOOL_CALL_WITHOUT_APPROVAL` are the two that matter most: they are the
    ordering statements the whole agent design rests on.
    """

    OUT_OF_ORDER = "out_of_order"
    MISSING_REQUEST = "missing_request"
    MISSING_OUTCOME = "missing_outcome"
    CONSEQUENCE_AFTER_OUTCOME = "consequence_after_outcome"
    DUPLICATE_TERMINAL = "duplicate_terminal"
    TOOL_CALL_WITHOUT_POLICY = "tool_call_without_policy"
    TOOL_CALL_WITHOUT_APPROVAL = "tool_call_without_approval"
    TOOL_CALL_WITHOUT_RESULT = "tool_call_without_result"
    RESULT_WITHOUT_CALL = "result_without_call"
    ITERATION_LIMIT_EXCEEDED = "iteration_limit_exceeded"
    TIME_WENT_BACKWARDS = "time_went_backwards"


# Events that CHANGE what happened, as opposed to recording where the record
# was left. Nothing in this set may follow the outcome of a turn; a workflow
# state transition may, because persisting the terminal state is genuinely the
# last thing that occurs and pretending otherwise would falsify the ordering.
CONSEQUENTIAL_EVENTS = frozenset(
    {
        TrajectoryEventKind.MODEL_DECISION,
        TrajectoryEventKind.POLICY_VERDICT,
        TrajectoryEventKind.APPROVAL_REQUESTED,
        TrajectoryEventKind.APPROVAL_DECIDED,
        TrajectoryEventKind.TOOL_CALL,
        TrajectoryEventKind.TOOL_RESULT,
    }
)


class TrajectoryEvent(BaseModel):
    """One step, described without repeating any of its content.

    Closed and frozen. A field this model does not declare is a validation
    failure rather than silently accepted data — which matters here because the
    tempting way to add content to an audit record is an `extra` key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    trajectory_id: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    sequence: int = Field(ge=1)
    kind: TrajectoryEventKind
    occurred_at: str = Field(min_length=1, description="UTC ISO-8601 wall clock.")
    elapsed_ms: float = Field(ge=0.0, description="Monotonic offset from the start of the turn.")

    workflow_id: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)

    # --- what the model proposed (untrusted, recorded as metadata only) -----
    decision_kind: DecisionKind | None = None
    claimed_risk_level: ToolRiskLevel | None = None
    risk_claim_mismatch: bool = False
    argument_count: int = Field(default=0, ge=0)
    question_chars: int = Field(default=0, ge=0)

    # --- what the application decided ---------------------------------------
    policy_decision: PolicyDecision | None = None
    denial_reason: DenialReason | None = None
    tool_name: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    tool_risk_level: ToolRiskLevel | None = None

    # --- approval ------------------------------------------------------------
    approval_id: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    approval_status: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    approval_reason: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    approver: str | None = Field(
        default=None,
        max_length=MAX_IDENTIFIER_LENGTH,
        description="Deciding principal. The one identity field; accountability requires it.",
    )

    # --- execution -----------------------------------------------------------
    tool_call_id: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    argument_fingerprint: str | None = Field(
        default=None,
        max_length=64,
        description="Hash over the validated arguments. Correlates an action, never reveals it.",
    )
    iteration: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    execution_status: ToolExecutionStatus | None = None
    failure_category: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    duration_ms: float | None = Field(default=None, ge=0.0)

    # --- workflow state ------------------------------------------------------
    from_state: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    to_state: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)

    # --- how it ended --------------------------------------------------------
    outcome: AgentOutcomeKind | None = None
    refusal_reason: RefusalReason | None = None
    citation_count: int = Field(default=0, ge=0)
    error_class: str | None = Field(
        default=None,
        max_length=MAX_IDENTIFIER_LENGTH,
        description="Exception class name only. Never a message: messages quote inputs.",
    )

    # --- cost ----------------------------------------------------------------
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialisable form, with unset fields omitted so a log line stays legible."""
        return self.model_dump(exclude_none=True)

    def as_otel_attributes(self) -> dict[str, object]:
        """The same event under OpenTelemetry GenAI semantic-convention names.

        Present so that exporting to Azure AI Foundry tracing — which is OTel
        GenAI conventions over Azure Monitor — is a SINK, not a
        re-instrumentation. The mapping lives here, next to the fields it maps,
        rather than inside an exporter that would then own a second definition
        of what each field means.

        Attributes outside the published conventions are namespaced under
        `agent.` so they are visibly this product's own and cannot be mistaken
        for a standard the backend will interpret.
        """
        attributes: dict[str, object] = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "platform-engineering-assistant",
            "agent.event.kind": self.kind.value,
            "agent.trajectory.id": self.trajectory_id,
            "agent.event.sequence": self.sequence,
        }
        if self.tool_name is not None:
            attributes["gen_ai.tool.name"] = self.tool_name
        if self.tool_call_id is not None:
            attributes["gen_ai.tool.call.id"] = self.tool_call_id
        if self.input_tokens is not None:
            attributes["gen_ai.usage.input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            attributes["gen_ai.usage.output_tokens"] = self.output_tokens

        optional = {
            "agent.workflow.id": self.workflow_id,
            "agent.decision.kind": self.decision_kind,
            "agent.policy.decision": self.policy_decision,
            "agent.policy.denial_reason": self.denial_reason,
            "agent.tool.risk_level": self.tool_risk_level,
            "agent.tool.claimed_risk_level": self.claimed_risk_level,
            "agent.tool.execution_status": self.execution_status,
            "agent.tool.failure_category": self.failure_category,
            "agent.approval.id": self.approval_id,
            "agent.approval.status": self.approval_status,
            "agent.approval.approver": self.approver,
            "agent.outcome": self.outcome,
            "agent.refusal_reason": self.refusal_reason,
            "agent.state.from": self.from_state,
            "agent.state.to": self.to_state,
            "agent.duration_ms": self.duration_ms,
        }
        for name, value in optional.items():
            if value is not None:
                attributes[name] = value.value if isinstance(value, StrEnum) else value
        return attributes


class TrajectoryDefect(BaseModel):
    """One violated invariant, and where it was violated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: TrajectoryDefectKind
    sequence: int = Field(ge=0, description="The offending event, or 0 for a whole-run defect.")
    detail: str = Field(description="Names the rule. Never quotes a value.")


class AgentTrajectory(BaseModel):
    """A complete recorded turn: header provenance plus ordered events.

    The provenance lives on the header rather than on every event because it is
    a property of the RUN — which prompt, which corpus, which model answered —
    and repeating it per event would invite two events to disagree about it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trajectory_id: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    workflow_id: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    events: tuple[TrajectoryEvent, ...] = ()

    agent_prompt_version: str = ""
    agent_prompt_hash: str = ""
    prompt_version: str = ""
    retrieval_config_version: str = ""
    corpus_version: int = 0

    provider: str = ""
    model: str | None = None
    deployment: str | None = None

    sink_failures: int = Field(
        default=0,
        ge=0,
        description="Sink emissions that raised. Recorded so a silent sink is not silent.",
    )

    @property
    def outcome(self) -> AgentOutcomeKind | None:
        for event in reversed(self.events):
            if event.kind is TrajectoryEventKind.OUTCOME:
                return event.outcome
        return None

    @property
    def total_ms(self) -> float:
        return self.events[-1].elapsed_ms if self.events else 0.0

    def of_kind(self, kind: TrajectoryEventKind) -> tuple[TrajectoryEvent, ...]:
        return tuple(event for event in self.events if event.kind is kind)

    def verify(self) -> tuple[TrajectoryDefect, ...]:
        """Check every ordering invariant. Deterministic, offline, no model.

        This is the definition of "trajectory correctness" that Phase 18.6
        measures. It is deliberately a property of the RECORD rather than of the
        code that produced it: a control that can only be demonstrated by
        reading the implementation cannot be evaluated per case, and a control
        that is not evaluated per case is one nobody notices losing.
        """
        defects: list[TrajectoryDefect] = []

        def defect(kind: TrajectoryDefectKind, sequence: int, detail: str) -> None:
            defects.append(TrajectoryDefect(kind=kind, sequence=sequence, detail=detail))

        if not self.events:
            defect(TrajectoryDefectKind.MISSING_REQUEST, 0, "the trajectory is empty")
            return tuple(defects)

        # --- ordering --------------------------------------------------------
        for index, event in enumerate(self.events, start=1):
            if event.sequence != index:
                defect(
                    TrajectoryDefectKind.OUT_OF_ORDER,
                    event.sequence,
                    "event sequence numbers must be contiguous from 1",
                )
        for earlier, later in zip(self.events, self.events[1:], strict=False):
            if later.elapsed_ms < earlier.elapsed_ms:
                defect(
                    TrajectoryDefectKind.TIME_WENT_BACKWARDS,
                    later.sequence,
                    "elapsed_ms decreased between consecutive events",
                )

        # --- the shape of a turn --------------------------------------------
        if self.events[0].kind is not TrajectoryEventKind.REQUEST_RECEIVED:
            defect(
                TrajectoryDefectKind.MISSING_REQUEST,
                self.events[0].sequence,
                "a trajectory must open with request_received",
            )

        outcomes = self.of_kind(TrajectoryEventKind.OUTCOME)
        if not outcomes:
            defect(TrajectoryDefectKind.MISSING_OUTCOME, 0, "a trajectory must record an outcome")
        else:
            if len(outcomes) > 1:
                defect(
                    TrajectoryDefectKind.DUPLICATE_TERMINAL,
                    outcomes[-1].sequence,
                    "a turn records exactly one outcome",
                )
            after = self.events[outcomes[0].sequence :]
            for event in after:
                if event.kind in CONSEQUENTIAL_EVENTS:
                    defect(
                        TrajectoryDefectKind.CONSEQUENCE_AFTER_OUTCOME,
                        event.sequence,
                        "a consequential event followed the outcome of the turn",
                    )

        # --- the two ordering statements the design rests on -----------------
        authorised: set[str] = set()
        approved_fingerprints: set[str] = set()
        awaiting_approval: set[str] = set()
        called: dict[str, TrajectoryEvent] = {}

        for event in self.events:
            if event.kind is TrajectoryEventKind.POLICY_VERDICT:
                if event.policy_decision is PolicyDecision.ALLOW and event.tool_name:
                    authorised.add(event.tool_name)
                if event.policy_decision is PolicyDecision.REQUIRE_APPROVAL and event.tool_name:
                    awaiting_approval.add(event.tool_name)

            elif event.kind is TrajectoryEventKind.APPROVAL_DECIDED:
                if event.approval_status == "approved" and event.argument_fingerprint:
                    approved_fingerprints.add(event.argument_fingerprint)

            elif event.kind is TrajectoryEventKind.TOOL_CALL:
                name = event.tool_name or ""
                # Authority comes from a policy verdict naming the tool, or —
                # on a resumed workflow, where policy ran in the interrupted
                # turn — from an approval granted for these exact arguments.
                carried = (event.argument_fingerprint or "") in approved_fingerprints
                if name not in authorised and name not in awaiting_approval and not carried:
                    defect(
                        TrajectoryDefectKind.TOOL_CALL_WITHOUT_POLICY,
                        event.sequence,
                        "a tool was called with no preceding policy verdict naming it",
                    )
                # A state-changing call needs an approval for THIS argument set,
                # not merely an approval somewhere earlier in the turn.
                if event.tool_risk_level is ToolRiskLevel.STATE_CHANGING:
                    fingerprint = event.argument_fingerprint or ""
                    if fingerprint not in approved_fingerprints:
                        defect(
                            TrajectoryDefectKind.TOOL_CALL_WITHOUT_APPROVAL,
                            event.sequence,
                            "a state-changing tool was called without an approval "
                            "matching its arguments",
                        )
                if event.iteration > MAX_TOOL_ITERATIONS:
                    defect(
                        TrajectoryDefectKind.ITERATION_LIMIT_EXCEEDED,
                        event.sequence,
                        f"iteration {event.iteration} exceeds the ceiling of {MAX_TOOL_ITERATIONS}",
                    )
                if event.tool_call_id:
                    called[event.tool_call_id] = event

            elif event.kind is TrajectoryEventKind.TOOL_RESULT:
                call_id = event.tool_call_id or ""
                if call_id not in called:
                    defect(
                        TrajectoryDefectKind.RESULT_WITHOUT_CALL,
                        event.sequence,
                        "a tool result names no recorded call",
                    )
                else:
                    called.pop(call_id, None)

        for pending in called.values():
            defect(
                TrajectoryDefectKind.TOOL_CALL_WITHOUT_RESULT,
                pending.sequence,
                "a tool call recorded no result",
            )

        return tuple(defects)

    @property
    def is_well_formed(self) -> bool:
        return not self.verify()


class TrajectorySink(Protocol):
    """Where events go. One method, so an implementation is hard to get wrong."""

    def emit(self, event: TrajectoryEvent) -> None: ...


class NullTrajectorySink:
    """Records nothing. The default, so observability is opt-in per deployment."""

    def emit(self, event: TrajectoryEvent) -> None:  # noqa: D102 - protocol implementation
        return None


class InMemoryTrajectorySink:
    """A bounded ring of recent trajectories, for inspection and tests.

    Bounded on purpose. An unbounded in-process buffer of every turn is a memory
    leak that presents itself as an observability feature, and the oldest turn is
    the one least likely to be under investigation.
    """

    def __init__(self, capacity: int = 200) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._order: deque[str] = deque()
        self._events: dict[str, list[TrajectoryEvent]] = {}

    def emit(self, event: TrajectoryEvent) -> None:
        with self._lock:
            if event.trajectory_id not in self._events:
                self._events[event.trajectory_id] = []
                self._order.append(event.trajectory_id)
                while len(self._order) > self._capacity:
                    self._events.pop(self._order.popleft(), None)
            self._events[event.trajectory_id].append(event)

    def events_for(self, trajectory_id: str) -> tuple[TrajectoryEvent, ...]:
        with self._lock:
            return tuple(self._events.get(trajectory_id, ()))

    def trajectory_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._order)


class JsonLinesTrajectorySink:
    """Append-only JSON Lines on local disk.

    Append-only because an audit trail that can be rewritten in place is not one.
    One line per event keeps a partially written file readable up to its last
    complete line, which is what a crash leaves behind.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: TrajectoryEvent) -> None:
        line = json.dumps(event.as_dict(), separators=(",", ":"), sort_keys=True)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class LoggingTrajectorySink:
    """Structured records through the standard logging stack.

    Uses the product's existing logger so trajectory events land wherever the
    application's logs already land — which in this platform is Log Analytics —
    without introducing a second shipping path to own.
    """

    def __init__(self, logger_name: str = "platform_engineering_assistant.agent") -> None:
        import logging

        self._logger = logging.getLogger(logger_name)

    def emit(self, event: TrajectoryEvent) -> None:
        self._logger.info("agent_trajectory_event", extra={"trajectory": event.as_dict()})


class CompositeTrajectorySink:
    """Fan out to several sinks, isolating each one's failures.

    A sink that raises is counted and skipped, and the remaining sinks still
    receive the event. Two sinks configured together must not mean either can
    silence the other, and neither may take the turn down with it.
    """

    def __init__(self, sinks: Iterable[TrajectorySink]) -> None:
        self._sinks = tuple(sinks)
        self._failures = 0
        self._lock = threading.Lock()

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def emit(self, event: TrajectoryEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:  # noqa: BLE001 - deliberate: telemetry never fails a turn
                with self._lock:
                    self._failures += 1


class TrajectoryRecorder:
    """Builds one trajectory, event by event, and forwards each to the sink.

    Sequence numbers and elapsed times are assigned HERE rather than by callers,
    so an instrumented code path cannot get the ordering wrong even by accident;
    a caller supplies what happened, never where it belongs.
    """

    def __init__(
        self,
        trajectory_id: str,
        sink: TrajectorySink | None = None,
        *,
        workflow_id: str | None = None,
        started: float | None = None,
    ) -> None:
        self._trajectory_id = trajectory_id
        self._sink = sink if sink is not None else NullTrajectorySink()
        self._workflow_id = workflow_id
        self._started = started if started is not None else time.perf_counter()
        self._lock = threading.Lock()
        self._events: list[TrajectoryEvent] = []
        self._sink_failures = 0

    @property
    def trajectory_id(self) -> str:
        return self._trajectory_id

    def bind_workflow(self, workflow_id: str) -> None:
        """Attach a workflow id once the workflow record exists."""
        self._workflow_id = workflow_id

    def record(self, kind: TrajectoryEventKind, **fields: object) -> TrajectoryEvent:
        """Append one event. Never raises on a sink failure."""
        with self._lock:
            sequence = len(self._events) + 1
            event = TrajectoryEvent(
                event_id=f"ev-{uuid.uuid4().hex[:16]}",
                trajectory_id=self._trajectory_id,
                sequence=sequence,
                kind=kind,
                occurred_at=datetime.now(UTC).isoformat(),
                elapsed_ms=max(0.0, (time.perf_counter() - self._started) * 1000.0),
                workflow_id=self._workflow_id,
                **fields,  # type: ignore[arg-type]
            )
            self._events.append(event)

        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 - a sink must never fail a turn
            with self._lock:
                self._sink_failures += 1
        return event

    def build(
        self,
        *,
        agent_prompt_version: str = "",
        agent_prompt_hash: str = "",
        prompt_version: str = "",
        retrieval_config_version: str = "",
        corpus_version: int = 0,
        provider: str = "",
        model: str | None = None,
        deployment: str | None = None,
    ) -> AgentTrajectory:
        """The completed record. Callers may build more than once; it is a view."""
        with self._lock:
            events = tuple(self._events)
            failures = self._sink_failures
        return AgentTrajectory(
            trajectory_id=self._trajectory_id,
            workflow_id=self._workflow_id,
            events=events,
            agent_prompt_version=agent_prompt_version,
            agent_prompt_hash=agent_prompt_hash,
            prompt_version=prompt_version,
            retrieval_config_version=retrieval_config_version,
            corpus_version=corpus_version,
            provider=provider,
            model=model,
            deployment=deployment,
            sink_failures=failures,
        )


__all__ = [
    "AgentTrajectory",
    "CONSEQUENTIAL_EVENTS",
    "CompositeTrajectorySink",
    "InMemoryTrajectorySink",
    "JsonLinesTrajectorySink",
    "LoggingTrajectorySink",
    "NullTrajectorySink",
    "TrajectoryDefect",
    "TrajectoryDefectKind",
    "TrajectoryEvent",
    "TrajectoryEventKind",
    "TrajectoryRecorder",
    "TrajectorySink",
]
