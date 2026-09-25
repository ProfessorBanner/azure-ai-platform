"""Human approval as a deterministic control, not a convention.

WHAT AN APPROVAL IS
-------------------
An approval authorises ONE action: one tool, with one exact set of validated
arguments, for a bounded period, granted by an identified human who is not the
thing that asked.

Each of those four qualifiers is a control, and each exists because dropping it
produces a specific failure:

  ONE TOOL, ONE ARGUMENT SET   Approving "a change request" rather than a
                               specific one lets the arguments be swapped after
                               the fact. The request therefore records a
                               fingerprint of the validated arguments, and
                               execution recomputes it. A single changed
                               character invalidates the approval.

  BOUNDED PERIOD               An approval that never expires becomes a standing
                               permission nobody remembers granting. Expiry is
                               evaluated at execution time, not by a sweeper, so
                               a stale record cannot be executed even if nothing
                               has run since it lapsed.

  IDENTIFIED HUMAN             An approval nobody signed cannot be audited.

  NOT THE THING THAT ASKED     A system that can approve its own requests has no
                               approval step, only a delay. This is enforced by
                               rejecting an approver equal to the requester and
                               any approver bearing a machine principal prefix.

APPROVAL IS NOT EXECUTION
-------------------------
`authorise_execution` returns a decision; it never runs anything. Duplicate
suppression remains Phase 18.2's idempotency store, so an approved action that
has already happened is still blocked by the key it claimed.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from platform_engineering_assistant.agent.domain import ToolRiskLevel
from platform_engineering_assistant.agent.execution import idempotency_key_for
from platform_engineering_assistant.agent.tools import ToolInput
from platform_engineering_assistant.agent.trajectory import (
    TrajectoryEventKind,
    TrajectoryRecorder,
    TrajectorySink,
)

DEFAULT_APPROVAL_TTL_SECONDS = 3600.0

# Principals that may never approve anything. The agent, its decider and its
# tools are all machines; an approval signed by one of them is not an approval.
MACHINE_PRINCIPAL_PREFIXES = ("agent:", "model:", "system:", "tool:", "req-", "agt-")


class ApprovalStatus(StrEnum):
    """Lifecycle of one approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalReason(StrEnum):
    """Why an approval reached its current state, or why execution was refused."""

    GRANTED_BY_HUMAN = "granted_by_human"
    DENIED_BY_HUMAN = "denied_by_human"
    CANCELLED_BY_REQUESTER = "cancelled_by_requester"
    TIME_EXPIRED = "time_expired"

    # Refusal reasons produced at execution time.
    NOT_APPROVED = "not_approved"
    ARGUMENTS_CHANGED = "arguments_changed"
    UNKNOWN_APPROVAL = "unknown_approval"
    SELF_APPROVAL_REJECTED = "self_approval_rejected"
    ALREADY_DECIDED = "already_decided"


class ApprovalError(Exception):
    """An approval decision could not be recorded."""

    def __init__(self, reason: ApprovalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RequestedAction(BaseModel):
    """Exactly what is being asked for. Immutable once recorded.

    `argument_fingerprint` is the Phase 18.2 idempotency key: a hash over the
    tool name and the canonical form of the VALIDATED arguments. Reusing it
    means the thing that identifies an action for duplicate suppression is the
    same thing that identifies it for approval — there is no way for the two to
    disagree about what "the same action" means.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1)
    argument_fingerprint: str = Field(min_length=64, max_length=64)
    summary: str = Field(description="Server-built description of the action, for a human.")


class ApprovalRequest(BaseModel):
    """One pending or decided approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(min_length=1)
    status: ApprovalStatus
    action: RequestedAction

    requested_by: str = Field(min_length=1, description="Agent request id that asked.")
    context_id: str = Field(default="", description="Correlation id for the originating turn.")
    requested_at: str
    expires_at: str

    decided_by: str | None = None
    decided_at: str | None = None
    reason: ApprovalReason | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        """Expiry is a property of the clock, not of a stored flag.

        Evaluated on read so that a lapsed approval cannot be executed even if
        nothing has run since it expired and no sweeper has visited it.
        """
        moment = now or datetime.now(UTC)
        return moment >= datetime.fromisoformat(self.expires_at)

    def effective_status(self, now: datetime | None = None) -> ApprovalStatus:
        """The status accounting for expiry."""
        if self.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED) and self.is_expired(
            now
        ):
            return ApprovalStatus.EXPIRED
        return self.status


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """The outcome of asking whether an action may execute now."""

    permitted: bool
    status: ApprovalStatus
    reason: ApprovalReason | None = None
    approval_id: str | None = None

    @property
    def refused(self) -> bool:
        return not self.permitted


class ApprovalStore(Protocol):
    """Persistence for approval requests."""

    def put(self, request: ApprovalRequest) -> None: ...

    def get(self, approval_id: str) -> ApprovalRequest | None: ...

    def list_pending(self) -> list[ApprovalRequest]: ...


class InMemoryApprovalStore:
    """Process-local store. No new infrastructure, by design.

    Honest about its limits: approvals do not survive a restart. Phase 18.4's
    durable workflow state is where that changes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}

    def put(self, request: ApprovalRequest) -> None:
        with self._lock:
            self._requests[request.approval_id] = request

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._requests.get(approval_id)

    def list_pending(self) -> list[ApprovalRequest]:
        with self._lock:
            requests = list(self._requests.values())
        return [
            request for request in requests if request.effective_status() is ApprovalStatus.PENDING
        ]


def is_machine_principal(principal: str) -> bool:
    """True when the principal is a machine and may never approve."""
    lowered = principal.strip().lower()
    return not lowered or any(lowered.startswith(prefix) for prefix in MACHINE_PRINCIPAL_PREFIXES)


class ApprovalService:
    """Creates approval requests and rules on whether an action may execute."""

    def __init__(
        self,
        store: ApprovalStore | None = None,
        ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
        trajectory_sink: TrajectorySink | None = None,
    ) -> None:
        self._store = store or InMemoryApprovalStore()
        self._ttl = timedelta(seconds=ttl_seconds)
        # Phase 18.5. The decision is recorded HERE rather than by the HTTP
        # route, so a decision reaches the audit trail whichever caller made
        # it. An approval recorded in one place and audited in another is how
        # the two eventually disagree.
        self._trajectory_sink = trajectory_sink

    @property
    def store(self) -> ApprovalStore:
        return self._store

    def request(
        self,
        *,
        tool_name: str,
        payload: ToolInput,
        summary: str,
        requested_by: str,
        context_id: str = "",
        now: datetime | None = None,
    ) -> ApprovalRequest:
        """Record a PENDING approval for one exact action."""
        moment = now or datetime.now(UTC)
        request = ApprovalRequest(
            approval_id=f"apr-{uuid.uuid4().hex[:16]}",
            status=ApprovalStatus.PENDING,
            action=RequestedAction(
                tool_name=tool_name,
                argument_fingerprint=idempotency_key_for(tool_name, payload),
                summary=summary,
            ),
            requested_by=requested_by,
            context_id=context_id,
            requested_at=moment.isoformat(),
            expires_at=(moment + self._ttl).isoformat(),
        )
        self._store.put(request)
        return request

    def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        approver: str,
        now: datetime | None = None,
    ) -> ApprovalRequest:
        """Record a human decision.

        Raises:
            ApprovalError: when the approval is unknown, already decided,
                expired, or when the approver is not permitted to decide it.
        """
        request = self._store.get(approval_id)
        if request is None:
            raise ApprovalError(ApprovalReason.UNKNOWN_APPROVAL, "No such approval request.")

        # --- the self-approval control -------------------------------------
        if is_machine_principal(approver):
            raise ApprovalError(
                ApprovalReason.SELF_APPROVAL_REJECTED,
                "A machine principal may not approve an action.",
            )
        if approver.strip() == request.requested_by:
            raise ApprovalError(
                ApprovalReason.SELF_APPROVAL_REJECTED,
                "The requester may not approve its own request.",
            )

        moment = now or datetime.now(UTC)
        effective = request.effective_status(moment)
        if effective is ApprovalStatus.EXPIRED:
            expired = request.model_copy(
                update={"status": ApprovalStatus.EXPIRED, "reason": ApprovalReason.TIME_EXPIRED}
            )
            self._store.put(expired)
            raise ApprovalError(ApprovalReason.TIME_EXPIRED, "The approval request has expired.")
        if effective is not ApprovalStatus.PENDING:
            raise ApprovalError(
                ApprovalReason.ALREADY_DECIDED, "The approval request is no longer pending."
            )

        decided = request.model_copy(
            update={
                "status": ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED,
                "decided_by": approver.strip(),
                "decided_at": moment.isoformat(),
                "reason": (
                    ApprovalReason.GRANTED_BY_HUMAN if approved else ApprovalReason.DENIED_BY_HUMAN
                ),
            }
        )
        self._store.put(decided)
        self._record_decision(decided)
        return decided

    def _record_decision(self, decided: ApprovalRequest) -> None:
        """Emit the decision as a trajectory event. Never fails the decision.

        The event is attributed to the ORIGINATING turn's correlation id, so a
        human decision taken minutes later still lands on the trajectory of the
        turn that asked for it.
        """
        if self._trajectory_sink is None:
            return
        recorder = TrajectoryRecorder(
            trajectory_id=decided.context_id or decided.requested_by,
            sink=self._trajectory_sink,
        )
        recorder.record(
            TrajectoryEventKind.APPROVAL_DECIDED,
            approval_id=decided.approval_id,
            approval_status=decided.status.value,
            approval_reason=decided.reason.value if decided.reason is not None else None,
            approver=decided.decided_by,
            tool_name=decided.action.tool_name,
            tool_risk_level=ToolRiskLevel.STATE_CHANGING,
            argument_fingerprint=decided.action.argument_fingerprint,
        )

    def cancel(self, approval_id: str, now: datetime | None = None) -> ApprovalRequest:
        """Withdraw a pending request. Terminal."""
        request = self._store.get(approval_id)
        if request is None:
            raise ApprovalError(ApprovalReason.UNKNOWN_APPROVAL, "No such approval request.")
        cancelled = request.model_copy(
            update={
                "status": ApprovalStatus.CANCELLED,
                "reason": ApprovalReason.CANCELLED_BY_REQUESTER,
                "decided_at": (now or datetime.now(UTC)).isoformat(),
            }
        )
        self._store.put(cancelled)
        return cancelled

    def authorise_execution(
        self,
        approval_id: str,
        *,
        tool_name: str,
        payload: ToolInput,
        now: datetime | None = None,
    ) -> ApprovalDecision:
        """May this exact action execute now?

        Recomputes the argument fingerprint from the payload actually being
        executed. Approving one action and executing another is the specific
        substitution this check exists to prevent, and it is why the comparison
        is against a hash of the validated arguments rather than against the
        tool name alone.
        """
        request = self._store.get(approval_id)
        if request is None:
            return ApprovalDecision(
                permitted=False,
                status=ApprovalStatus.DENIED,
                reason=ApprovalReason.UNKNOWN_APPROVAL,
            )

        status = request.effective_status(now)
        if status is not ApprovalStatus.APPROVED:
            return ApprovalDecision(
                permitted=False,
                status=status,
                reason=(
                    ApprovalReason.TIME_EXPIRED
                    if status is ApprovalStatus.EXPIRED
                    else ApprovalReason.NOT_APPROVED
                ),
                approval_id=approval_id,
            )

        if request.action.tool_name != tool_name:
            return ApprovalDecision(
                permitted=False,
                status=status,
                reason=ApprovalReason.ARGUMENTS_CHANGED,
                approval_id=approval_id,
            )

        if request.action.argument_fingerprint != idempotency_key_for(tool_name, payload):
            return ApprovalDecision(
                permitted=False,
                status=status,
                reason=ApprovalReason.ARGUMENTS_CHANGED,
                approval_id=approval_id,
            )

        return ApprovalDecision(permitted=True, status=status, approval_id=approval_id)


__all__ = [
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "ApprovalDecision",
    "ApprovalError",
    "ApprovalReason",
    "ApprovalRequest",
    "ApprovalService",
    "ApprovalStatus",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "RequestedAction",
    "is_machine_principal",
]
