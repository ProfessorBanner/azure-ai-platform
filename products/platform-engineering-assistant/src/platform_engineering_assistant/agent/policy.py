"""The deterministic policy layer: pure functions, no model involvement.

WHAT THIS IS
------------
`authorise` is a total function from (proposal, registry) to a verdict. It reads
the registry, parses the arguments against the tool's typed input, and returns
ALLOW, REQUIRE_APPROVAL or DENY. No I/O, no clock, no randomness, no network.

WHY NOT ASK A MODEL WHETHER A CALL IS SAFE
-------------------------------------------
Because the answer would be a guess dressed as a ruling, produced by the same
class of system whose proposal is being checked, and susceptible to the same
prompt injection that may have produced the proposal. An attacker who can
influence the proposal can influence the reviewer. A lookup in a
version-controlled table cannot be talked round.

THE RULES, IN ORDER
-------------------
    refuse / no_tool          -> ALLOW           (nothing to authorise)
    tool name missing         -> DENY
    tool not registered       -> DENY
    tool not allow-listed     -> DENY
    arguments do not validate -> DENY
    risk READ_ONLY            -> ALLOW
    risk STATE_CHANGING       -> REQUIRE_APPROVAL

Order matters. Registration is checked before arguments so that an unknown tool
never has its arguments parsed — there is no input model to parse them against,
and attempting it would be the first step towards inventing one.

FAIL CLOSED
-----------
Every unrecognised condition denies. There is no default-allow branch, and the
final `else` is a denial rather than a fallthrough.
"""

from __future__ import annotations

from platform_engineering_assistant.agent.domain import (
    AgentDecision,
    DecisionKind,
    DenialReason,
    PolicyDecision,
    PolicyVerdict,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.registry import ToolRegistry
from platform_engineering_assistant.agent.tools import parse_or_none


def authorise(decision: AgentDecision, registry: ToolRegistry) -> PolicyVerdict:
    """Rule on one proposal. Deterministic and side-effect free.

    The model's `claimed_risk_level` is never read here. That is the point of
    the function: risk comes from `registry.risk_of(name)` and nowhere else.
    """
    if decision.kind is not DecisionKind.USE_TOOL:
        # Nothing is being invoked, so there is nothing to authorise. The
        # grounding policy still governs whatever answer follows.
        return PolicyVerdict(decision=PolicyDecision.ALLOW)

    name = (decision.tool_name or "").strip()
    if not name:
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            denial_reason=DenialReason.MISSING_TOOL_NAME,
            detail="A tool invocation must name a tool.",
        )

    definition = registry.get(name)
    if definition is None:
        # Named something that does not exist. Never attempt to guess which real
        # tool was meant: a near-miss resolved generously is how a model reaches
        # a tool it was not offered.
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            tool_name=name,
            denial_reason=DenialReason.UNKNOWN_TOOL,
            detail="The named tool is not registered.",
        )

    if not definition.allowed:
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            tool_name=name,
            risk_level=definition.risk,
            denial_reason=DenialReason.NOT_ALLOW_LISTED,
            detail="The named tool is registered but not permitted in this deployment.",
        )

    if parse_or_none(definition.tool, decision.arguments) is None:
        # The detail names the RULE, never the offending value: arguments are
        # model output derived from a user question, and this text is logged.
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            tool_name=name,
            risk_level=definition.risk,
            denial_reason=DenialReason.INVALID_ARGUMENTS,
            detail="The supplied arguments do not satisfy the tool's input contract.",
        )

    if definition.risk is ToolRiskLevel.READ_ONLY:
        return PolicyVerdict(
            decision=PolicyDecision.ALLOW,
            tool_name=name,
            risk_level=ToolRiskLevel.READ_ONLY,
            detail="Read-only tool permitted.",
        )

    if definition.risk is ToolRiskLevel.STATE_CHANGING:
        return PolicyVerdict(
            decision=PolicyDecision.REQUIRE_APPROVAL,
            tool_name=name,
            risk_level=ToolRiskLevel.STATE_CHANGING,
            detail="State-changing tool requires human approval before execution.",
        )

    # Unreachable while ToolRiskLevel has two members, and a denial rather than
    # an assertion so that ADDING a risk level fails closed instead of crashing.
    return PolicyVerdict(
        decision=PolicyDecision.DENY,
        tool_name=name,
        denial_reason=DenialReason.UNKNOWN_TOOL,
        detail="The tool's risk classification is not recognised.",
    )


__all__ = ["authorise"]
