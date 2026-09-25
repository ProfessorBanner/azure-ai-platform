"""One correlated record per turn, joining the managed and application sides.

WHAT IT JOINS
-------------
    agent_name / agent_version   the immutable definition that ran   MANAGED
    conversation_id              the session                         MANAGED
    response_ids                 each step within it                 MANAGED
    tool calls                   name, outcome, risk                 APPLICATION
    policy events                verdict, denial reason              APPLICATION
    approval_id                  the human gate                      APPLICATION
    outcome                      how the turn ended                  APPLICATION

Without this, the two halves are only joinable by timestamp, which is not a
join. With it, "which definition, which conversation, which policy verdict, and
did a human approve" is one record.

WHAT IT DELIBERATELY OMITS
--------------------------
No chain-of-thought — it is not collected anywhere in this lab, so it cannot
leak. No credentials, no endpoint, no token. No prompt text: the instructions
are identified by version and length, never reproduced. The user's QUESTION is
recorded as a length, not as text, matching the Phase 18 telemetry rule.

The one deliberate exception is `validated_arguments`, carried for the reason
19.1b records: a comparison whose own record is redacted cannot show which side
lost information. That is lab-only and must be revisited before any durable
sink, since a search query is the user's question restated.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from foundry_agent_lab.governance import GovernedOutcome
from foundry_agent_lab.governed_runtime import GovernedTurn

FORBIDDEN_FIELDS = frozenset(
    {
        "question",
        "prompt",
        "instructions",
        "answer",
        "reasoning",
        "chain_of_thought",
        "scratchpad",
        "credential",
        "token",
        "api_key",
        "endpoint",
    }
)


@dataclass(frozen=True, slots=True)
class ToolEvent:
    """One proposed call, and what the application decided about it."""

    tool_name: str
    outcome: str
    risk: str | None = None
    policy_decision: str | None = None
    denial_reason: str | None = None
    approval_id: str | None = None
    argument_fingerprint: str | None = None
    tool_call_id: str | None = None
    validated_arguments: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnCorrelation:
    """The whole turn, joinable end to end."""

    turn_id: str
    agent_name: str
    agent_version: str
    conversation_id: str
    response_ids: tuple[str, ...]
    tool_events: tuple[ToolEvent, ...]
    outcome: str
    question_chars: int
    final_text_chars: int
    workflow_ids: tuple[str, ...] = ()
    model: str | None = None
    total_tokens: int | None = None
    call_limit_reached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, default=str)


def turn_outcome(turn: GovernedTurn) -> str:
    """How the turn ended, as one word a gate can be written against."""
    if turn.approval_required:
        return "approval_required"
    if any(r.outcome is GovernedOutcome.DENIED for r in turn.results):
        return "denied"
    if any(r.outcome is GovernedOutcome.MALFORMED for r in turn.results):
        return "malformed"
    if turn.final_text:
        return "answered"
    return "failed"


def correlate(turn: GovernedTurn, question: str) -> TurnCorrelation:
    """Build the correlation record. The question contributes only its length."""
    return TurnCorrelation(
        turn_id=turn.turn_id,
        agent_name=turn.agent_name,
        agent_version=turn.agent_version,
        conversation_id=turn.conversation_id,
        response_ids=turn.response_ids,
        tool_events=tuple(
            ToolEvent(
                tool_name=r.tool_name,
                outcome=r.outcome.value,
                risk=r.risk,
                policy_decision=r.policy_decision,
                denial_reason=r.denial_reason,
                approval_id=r.approval_id,
                argument_fingerprint=r.argument_fingerprint,
                tool_call_id=r.tool_call_id,
                validated_arguments=dict(r.validated_arguments),
            )
            for r in turn.results
        ),
        outcome=turn_outcome(turn),
        question_chars=len(question),
        final_text_chars=len(turn.final_text),
        workflow_ids=turn.workflow_ids,
        model=turn.model,
        total_tokens=turn.total_tokens,
        call_limit_reached=turn.call_limit_reached,
    )


def verify(record: TurnCorrelation) -> tuple[str, ...]:
    """Trajectory validity: the invariants a correlated turn must satisfy.

    Deterministic and offline, exactly as Phase 18's `AgentTrajectory.verify()`
    is, and used as an evaluation gate rather than only as an assertion.
    """
    defects: list[str] = []

    if not record.conversation_id:
        defects.append("missing_conversation_id")
    if not record.response_ids:
        defects.append("missing_response_ids")
    if record.agent_version in ("", "unprovisioned"):
        defects.append("turn_ran_against_no_provisioned_version")

    for event in record.tool_events:
        if event.outcome == "executed":
            if event.risk != "read_only":
                defects.append("state_changing_tool_executed")
            if event.policy_decision != "allow":
                defects.append("execution_without_allow")
            if not event.tool_call_id:
                defects.append("execution_without_call_id")
        if event.outcome == "approval_required":
            if event.tool_call_id:
                defects.append("approval_required_but_a_call_ran")
            if not event.approval_id or not event.argument_fingerprint:
                defects.append("approval_without_binding")
        if event.outcome == "denied" and not event.denial_reason:
            defects.append("denial_without_reason")

    if record.outcome == "approval_required" and not record.workflow_ids:
        defects.append("approval_without_workflow")
    return tuple(defects)


__all__ = [
    "FORBIDDEN_FIELDS",
    "ToolEvent",
    "TurnCorrelation",
    "correlate",
    "turn_outcome",
    "verify",
]
