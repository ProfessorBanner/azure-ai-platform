"""The bridge from a Foundry function call to the Phase 18 controls.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not decide anything. Every ruling here is made by product code:

    registry     platform_engineering_assistant.agent.registry   risk, allow-list
    policy       platform_engineering_assistant.agent.policy     authorise()
    approval     platform_engineering_assistant.agent.approval   request/decide/authorise_execution
    execution    platform_engineering_assistant.agent.execution  timeout, retry, idempotency

The lab supplies only the adapter: it turns an untrusted Foundry function call
into the product's own `AgentDecision`, hands it to the product's `authorise`,
and does what the verdict says. That is the whole point of 19.1c — a
state-changing action proposed through managed orchestration is governed by
exactly the same controls as one proposed through the application's own loop,
because it reaches the same functions.

RISK NEVER COMES FROM THE CALL
------------------------------
`ToolRiskLevel` is read from the product registry. A Foundry proposal carries a
tool name and arguments and nothing else; there is no field for risk, and an
argument named `risk` is rejected as unexpected before policy is consulted. A
model that decides `propose_change_request` is read-only changes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from foundry_agent_lab._product import ensure_product_importable
from foundry_agent_lab.protocol import ProposedCall

ensure_product_importable()

from platform_engineering_assistant.agent.approval import (  # noqa: E402
    ApprovalService,
    InMemoryApprovalStore,
)
from platform_engineering_assistant.agent.domain import (  # noqa: E402
    AgentDecision,
    DecisionKind,
    PolicyDecision,
    ToolArgument,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.execution import (  # noqa: E402
    ExecutionStatus,
    ToolExecutor,
)
from platform_engineering_assistant.agent.policy import authorise  # noqa: E402
from platform_engineering_assistant.agent.registry import (  # noqa: E402
    build_registry as build_product_registry,
)
from platform_engineering_assistant.agent.tools import (  # noqa: E402
    ComponentLookupInput,
    LookupPlatformComponentTool,
    ProposeChangeInput,
    ProposeChangeRequestTool,
    SearchDocsInput,
    SearchPlatformDocsTool,
)


class GovernedOutcome(StrEnum):
    """What the application decided about one proposed call."""

    EXECUTED = "executed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class GovernedResult:
    """One proposal, and what the Phase 18 controls did about it."""

    outcome: GovernedOutcome
    tool_name: str
    risk: str | None = None
    policy_decision: str | None = None
    denial_reason: str | None = None
    validated_arguments: dict[str, str] = field(default_factory=dict)
    argument_fingerprint: str | None = None
    approval_id: str | None = None
    approval_summary: str | None = None
    tool_call_id: str | None = None
    output: str = ""
    detail: str = ""

    @property
    def executed(self) -> bool:
        return self.outcome is GovernedOutcome.EXECUTED


def as_agent_decision(call: ProposedCall) -> AgentDecision:
    """Turn an untrusted Foundry call into the product's own proposal type.

    Routed through `AgentDecision` deliberately: a call the product's wire model
    would have rejected must not be able to reach policy by arriving over a
    different transport. Duplicate argument names, over-long values and
    over-large argument sets are all refused here, by the product's validator.

    Raises:
        ValueError: when the call cannot be expressed as a valid proposal.
    """
    arguments = call.arguments
    if not isinstance(arguments, dict):
        raise ValueError("function call arguments were not an object")
    pairs = [ToolArgument(name=str(k), value=str(v)) for k, v in arguments.items()]
    return AgentDecision(
        kind=DecisionKind.USE_TOOL, tool_name=call.name or None, tool_arguments=pairs
    )


class GovernedToolGateway:
    """Applies the Phase 18 registry, policy, approval and executor to a call."""

    def __init__(
        self,
        index: Any,
        approvals: ApprovalService | None = None,
        executor: ToolExecutor | None = None,
    ) -> None:
        # The PRODUCT registry, not a lab one: same tools, same risk, same
        # allow-list. `propose_change_request` is state-changing here because
        # the product says so.
        self._registry = build_product_registry(index)
        self._approvals = approvals or ApprovalService(InMemoryApprovalStore())
        self._executor = executor or ToolExecutor()

    @property
    def registry(self) -> Any:
        return self._registry

    @property
    def approvals(self) -> ApprovalService:
        return self._approvals

    @property
    def executor(self) -> ToolExecutor:
        return self._executor

    def _payload(self, tool: Any, arguments: dict[str, str]) -> Any:
        """Parse into the tool's own typed input. Policy already validated it."""
        if isinstance(tool, SearchPlatformDocsTool):
            return SearchDocsInput.model_validate(arguments)
        if isinstance(tool, LookupPlatformComponentTool):
            return ComponentLookupInput.model_validate(arguments)
        if isinstance(tool, ProposeChangeRequestTool):
            return ProposeChangeInput.model_validate(arguments)
        raise ValueError(f"no execution binding for {type(tool).__name__}")

    def govern(self, call: ProposedCall, requested_by: str) -> GovernedResult:
        """Rule on one proposed call. Executes only a read-only ALLOW."""
        try:
            decision = as_agent_decision(call)
        except Exception as error:  # noqa: BLE001 - any malformed proposal
            return GovernedResult(
                outcome=GovernedOutcome.MALFORMED,
                tool_name=call.name,
                detail=type(error).__name__,
                output="REFUSED: the proposed call was malformed.",
            )

        verdict = authorise(decision, self._registry)
        risk = verdict.risk_level.value if verdict.risk_level else None

        if verdict.decision is PolicyDecision.DENY:
            return GovernedResult(
                outcome=GovernedOutcome.DENIED,
                tool_name=call.name,
                risk=risk,
                policy_decision=verdict.decision.value,
                denial_reason=verdict.denial_reason.value if verdict.denial_reason else None,
                detail=verdict.detail,
                output="REFUSED: that call was not authorised.",
            )

        definition = self._registry.get(verdict.tool_name or "")
        arguments = decision.arguments
        payload = self._payload(definition.tool, arguments)

        if verdict.decision is PolicyDecision.REQUIRE_APPROVAL:
            # STOP. Nothing executes. The approval records the exact tool and a
            # fingerprint of the validated arguments, so a later execution of a
            # different action cannot borrow this approval.
            summary = ProposeChangeRequestTool.summarise(payload)
            request = self._approvals.request(
                tool_name=definition.name,
                payload=payload,
                summary=summary,
                requested_by=requested_by,
                context_id=requested_by,
            )
            return GovernedResult(
                outcome=GovernedOutcome.APPROVAL_REQUIRED,
                tool_name=definition.name,
                risk=risk,
                policy_decision=verdict.decision.value,
                validated_arguments=dict(arguments),
                argument_fingerprint=request.action.argument_fingerprint,
                approval_id=request.approval_id,
                approval_summary=summary,
                output=(
                    "APPROVAL_REQUIRED: this action is state-changing and has been "
                    "recorded for human approval. Nothing has been executed."
                ),
            )

        # ALLOW: read-only only. Defence in depth — policy already guaranteed
        # this, and a refactor that broke it must fail loudly rather than run.
        if definition.risk is not ToolRiskLevel.READ_ONLY:
            raise AssertionError("a state-changing tool reached the execution path")

        tool_call_id = f"tc-{uuid.uuid4().hex[:16]}"
        outcome = self._executor.execute(definition, payload, tool_call_id=tool_call_id)
        if outcome.status is not ExecutionStatus.SUCCEEDED:
            return GovernedResult(
                outcome=GovernedOutcome.DENIED,
                tool_name=definition.name,
                risk=risk,
                policy_decision=verdict.decision.value,
                validated_arguments=dict(arguments),
                tool_call_id=outcome.tool_call_id,
                detail=str(outcome.failure_category),
                output="REFUSED: the tool did not complete.",
            )

        from foundry_agent_lab.docs_search import render_from_chunks

        return GovernedResult(
            outcome=GovernedOutcome.EXECUTED,
            tool_name=definition.name,
            risk=risk,
            policy_decision=verdict.decision.value,
            validated_arguments=dict(arguments),
            tool_call_id=outcome.tool_call_id,
            output=render_from_chunks(definition.tool, payload),
        )


__all__ = [
    "GovernedOutcome",
    "GovernedResult",
    "GovernedToolGateway",
    "as_agent_decision",
]
