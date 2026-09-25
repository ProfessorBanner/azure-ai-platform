"""Rendering a Phase 18 outcome for the Foundry Responses protocol.

WHAT IS LEFT HERE AFTER ADOPTING THE OFFICIAL SERVER
-----------------------------------------------------
Only two pure functions: the assistant text, and the structured metadata. The
protocol itself — SSE lifecycle, response ids, `/responses` routing, readiness,
cancellation, the response store — belongs to
`azure-ai-agentserver-responses` and is no longer imitated here. Phase 19.1e
originally hand-built a Responses payload; that was a plausible guess at a
contract the platform owns, and it has been deleted rather than maintained.

WHY THE OUTCOME LIVES IN METADATA
---------------------------------
A Responses payload has one natural place for text and no natural place for
"this stopped for human approval". Flattening `approval_required` into prose
would leave a caller unable to distinguish "recorded for a human" from "done" —
the exact confusion Phase 18 refused to create when it made `approval_required`
a first-class outcome rather than an error.

`ResponseObject` is a TypedDict carrying an optional `metadata` field, and
`TextResponse` accepts a `configure` callback that receives it, so the official
server CAN carry this. Values are stringified because response metadata is a
string map by convention; a caller branches on `metadata["outcome"]`, never on
the English.
"""

from __future__ import annotations

from platform_engineering_assistant.agent.domain import AgentOutcomeKind, AgentResponse


def text_for(response: AgentResponse) -> str:
    """What the caller reads. Never claims an action was performed."""
    if response.outcome is AgentOutcomeKind.ANSWERED:
        return response.answer or ""
    if response.outcome is AgentOutcomeKind.APPROVAL_REQUIRED:
        summary = response.approval_summary or "a state-changing action"
        return (
            "This request requires human approval and has NOT been carried out. "
            f"Recorded for approval: {summary}."
        )
    if response.outcome is AgentOutcomeKind.REFUSED:
        return f"I cannot answer this from the approved documentation ({response.refusal_reason})."
    if response.outcome is AgentOutcomeKind.DENIED:
        return "That request was not authorised."
    return "The request could not be completed."


def metadata_for(response: AgentResponse) -> dict[str, str]:
    """The structured facts a caller may branch on.

    Identifiers, enums and counts. No answer text, no arguments, no evidence —
    the same redaction posture the product's own telemetry keeps.
    """
    values: dict[str, object] = {
        "request_id": response.request_id,
        "outcome": response.outcome.value,
        "policy_decision": response.policy_decision.value if response.policy_decision else "",
        "selected_tool": response.selected_tool or "",
        "tool_risk_level": response.tool_risk_level.value if response.tool_risk_level else "",
        "tool_execution_status": response.tool_execution_status.value,
        "tool_iterations": response.tool_iterations,
        "denial_reason": response.denial_reason.value if response.denial_reason else "",
        "refusal_reason": response.refusal_reason.value if response.refusal_reason else "",
        "approval_id": response.approval_id or "",
        "citation_count": len(response.citations),
        "agent_prompt_version": response.agent_prompt_version,
        "prompt_version": response.prompt_version,
        "corpus_version": response.corpus_version,
    }
    return {key: str(value) for key, value in values.items()}


__all__ = ["metadata_for", "text_for"]
