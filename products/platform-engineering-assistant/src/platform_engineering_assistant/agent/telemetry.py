"""Safe agent telemetry.

REDACTION IS STRUCTURAL, AS EVERYWHERE ELSE IN THIS PRODUCT
------------------------------------------------------------
There is no field for the question, the answer, the tool ARGUMENTS, the evidence
or any reasoning. Tool arguments are singled out because they are the tempting
one: they are short, they look like structured metadata, and they are derived
from a user question — a `search_platform_docs` query is the question restated.
So the record carries the argument COUNT and never the arguments.

What it does carry: identifiers, enums, counts, durations and versions.

`claimed_risk_level` is recorded deliberately. It is the model's assertion about
a tool's risk, which the policy layer ignores; recording it is how a systematic
attempt to misclassify tools becomes visible instead of invisible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from platform_engineering_assistant.agent.domain import (
    AgentOutcomeKind,
    DecisionKind,
    DenialReason,
    PolicyDecision,
    ToolExecutionStatus,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.execution import ToolFailureCategory
from platform_engineering_assistant.domain import RefusalReason


@dataclass(frozen=True, slots=True)
class AgentTelemetry:
    """One agent turn, described without repeating any of its content."""

    request_id: str

    decision_kind: DecisionKind | None = None
    selected_tool: str | None = None
    tool_risk_level: ToolRiskLevel | None = None
    claimed_risk_level: ToolRiskLevel | None = None
    risk_claim_mismatch: bool = False

    policy_decision: PolicyDecision | None = None
    denial_reason: DenialReason | None = None

    tool_execution_status: ToolExecutionStatus = ToolExecutionStatus.NOT_EXECUTED
    tool_duration_ms: float = 0.0
    tool_failure_category: ToolFailureCategory | None = None
    tool_attempts: int = 0
    tool_call_id: str | None = None
    tool_argument_count: int = 0
    tool_iterations: int = 0
    # Chunk IDENTIFIERS, never chunk text. Recorded so citation containment can
    # be re-checked INDEPENDENTLY of the grounding policy that enforced it —
    # an audit rather than a restatement, exactly as the answering telemetry does.
    retrieved_chunk_ids: tuple[str, ...] = ()

    outcome: AgentOutcomeKind | None = None
    refusal_reason: RefusalReason | None = None
    citation_count: int = 0

    decision_ms: float = 0.0
    answer_ms: float = 0.0
    total_ms: float = 0.0

    question_chars: int = 0
    agent_prompt_version: str = ""
    agent_prompt_hash: str = ""
    prompt_version: str = ""
    retrieval_config_version: str = ""
    corpus_version: int = 0

    provider: str = ""
    model: str | None = None
    deployment: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def summary(self) -> str:
        """One-line, log-safe rendering."""
        parts = [
            f"request_id={self.request_id}",
            f"outcome={self.outcome}",
            f"decision={self.decision_kind}",
            f"tool={self.selected_tool or 'none'}",
            f"risk={self.tool_risk_level or 'n/a'}",
            f"policy={self.policy_decision}",
            f"exec={self.tool_execution_status}",
            f"iterations={self.tool_iterations}",
            f"total_ms={self.total_ms:.1f}",
        ]
        if self.risk_claim_mismatch:
            # Surfaced loudly: the model asserted a risk level the registry
            # disagrees with. Never load-bearing, always worth seeing.
            parts.append(f"RISK_CLAIM_MISMATCH claimed={self.claimed_risk_level}")
        if self.denial_reason is not None:
            parts.append(f"denial={self.denial_reason}")
        if self.tool_failure_category is not None:
            parts.append(f"tool_failure={self.tool_failure_category} attempts={self.tool_attempts}")
        if self.total_tokens is not None:
            parts.append(f"total_tokens={self.total_tokens}")
        return " ".join(parts)

    def as_dict(self) -> dict[str, object]:
        """Serialisable form for a structured log sink."""
        return asdict(self)


__all__ = ["AgentTelemetry"]
