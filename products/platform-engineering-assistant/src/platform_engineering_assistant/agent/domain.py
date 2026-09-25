"""Agent domain contracts.

THE CENTRAL ASYMMETRY
---------------------
The model PROPOSES. The application DECIDES.

Every type here is shaped by that split. `AgentDecision` is untrusted model
output and carries only what a proposal needs: which tool, with what arguments,
or none. `PolicyDecision` and `AgentOutcome` are produced by the server and are
the only things a caller may act on.

RISK IS A PROPERTY OF THE TOOL, NOT OF THE PROPOSAL
---------------------------------------------------
`ToolRiskLevel` is never read from model output. It is owned by the tool
registry, which is server-side, version-controlled and immutable at runtime. A
model that announces a state-changing tool is "read only" changes nothing: the
registry is consulted, the claim is recorded for telemetry, and policy runs on
the registry's classification. This is the single most important invariant in
the agent layer, and it is tested directly.

NO HIDDEN REASONING
-------------------
There is deliberately no field for chain-of-thought, scratchpad, deliberation or
explanation-of-self. Three reasons, and the third is the one that decides it:
such text is unvalidated model output that tends to leak prompt content; it
invites callers to treat a plausible narrative as justification; and it would
have to be redacted from every log and response, which is a rule someone
eventually forgets. The field simply does not exist.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from platform_engineering_assistant.domain import (
    QUESTION_MAX_LENGTH,
    QUESTION_MIN_LENGTH,
    Citation,
    ModelMetadata,
    RefusalReason,
    TokenUsage,
)

# Bounds on what a model may propose. Deliberately tight: these are the shape of
# untrusted input, not a capability budget.
MAX_TOOL_ARGUMENTS = 8
MAX_ARGUMENT_KEY_LENGTH = 64
MAX_ARGUMENT_VALUE_LENGTH = 2000
MAX_TOOL_NAME_LENGTH = 64

# The loop is bounded by construction. Two iterations is enough for the only
# shape this phase supports — consult a tool, then answer from what it returned —
# and an unbounded loop against a metered model is a cost and latency incident
# waiting for a bad decision to trigger it.
MAX_TOOL_ITERATIONS = 2


class ToolRiskLevel(StrEnum):
    """How consequential a tool is. Server-owned; never read from the model."""

    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"


class DecisionKind(StrEnum):
    """What the model proposes to do."""

    NO_TOOL = "no_tool"
    USE_TOOL = "use_tool"
    REFUSE = "refuse"


class PolicyDecision(StrEnum):
    """What the application authorises. Produced only by the policy layer."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class DenialReason(StrEnum):
    """Why a proposal was denied. Recorded; never used to coach the model."""

    UNKNOWN_TOOL = "unknown_tool"
    NOT_ALLOW_LISTED = "not_allow_listed"
    INVALID_ARGUMENTS = "invalid_arguments"
    MISSING_TOOL_NAME = "missing_tool_name"
    ITERATION_LIMIT = "iteration_limit"


class ToolExecutionStatus(StrEnum):
    """What happened when a tool ran."""

    NOT_EXECUTED = "not_executed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentOutcomeKind(StrEnum):
    """How the agent turn ended.

    `APPROVAL_REQUIRED` is a first-class outcome, not an error. A consequential
    action correctly stopping for a human is the system working, and modelling
    it as a failure would push callers towards retrying it.
    """

    ANSWERED = "answered"
    REFUSED = "refused"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"
    FAILED = "failed"


class AgentRequest(BaseModel):
    """A question for the agent. One field, exactly as `AnswerRequest`.

    Tool selection, tool arguments, the allow-list and the iteration budget are
    all server-owned. A caller who could name the tool could select the
    state-changing one directly, which is precisely the decision the policy
    layer exists to make.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(
        min_length=QUESTION_MIN_LENGTH,
        max_length=QUESTION_MAX_LENGTH,
        description="The question to answer from approved repository documentation.",
    )

    @model_validator(mode="after")
    def question_must_not_be_only_whitespace(self) -> AgentRequest:
        if not self.question.strip():
            raise ValueError("question must contain non-whitespace characters")
        return self


# WHY ARGUMENTS ARE A LIST OF PAIRS AND NOT `dict[str, str]`
# ----------------------------------------------------------
# Strict Structured Outputs requires every object property to be declared up
# front with `additionalProperties: false`. A map of arbitrary keys is the one
# shape it cannot express: Pydantic renders it as
# `additionalProperties: {"type": "string"}`, and a bound on it renders as
# `maxProperties`, which the Responses API rejects outright with
# `invalid_json_schema` — observed live against gpt-4-1-mini. A LIST of declared
# pairs says the same thing in a shape the strict schema can carry, so the wire
# encoding changes and nothing above it does.
#
# This is a change of ENCODING, not of trust. The pairs are still untrusted
# model output, still closed, still frozen, and still validated by the tool's
# own typed input model before anything runs.
#
# NOTE ON DOCSTRINGS: every model docstring below is emitted into the JSON
# schema as `description` and is therefore sent on EVERY request. The reasoning
# lives in comments like this one, which are not; the docstrings stay short
# because they are billed per call.
class ToolArgument(BaseModel):
    """One proposed argument, as a name/value pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    value: str


class AgentDecision(BaseModel):
    """UNTRUSTED model output: a proposal, nothing more.

    Closed and frozen. An invented field is a validation failure rather than
    silently discarded data — which matters here more than anywhere else,
    because an invented field is exactly how a model would try to smuggle in an
    authorisation it was never given.

    Arguments are stringly-typed pairs; each tool parses them into its own typed
    input model and rejects anything that does not fit.
    """

    # Bounds are enforced in the validator, NOT as Field constraints. A
    # `max_length` on the list renders as `maxItems` and on a string as
    # `maxLength` — schema keywords of exactly the kind the provider rejected.
    # Checking them below keeps the limits real and the emitted schema portable.

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DecisionKind
    tool_name: str | None = Field(
        default=None,
        max_length=MAX_TOOL_NAME_LENGTH,
        description="Name of a tool from the supplied catalogue. Verified against the registry.",
    )
    tool_arguments: list[ToolArgument] = Field(
        default_factory=list,
        description="Arguments for the tool. Parsed and validated by the tool itself.",
    )
    refusal_reason: RefusalReason | None = Field(
        default=None, description="Why the model declined. Only when kind is refuse."
    )
    claimed_risk_level: ToolRiskLevel | None = Field(
        default=None,
        description=(
            "What the model ASSERTS about the tool's risk. Recorded for telemetry and "
            "NEVER consulted by policy — risk is owned by the tool registry. Present so "
            "that a false claim is observable rather than invisible."
        ),
    )

    @model_validator(mode="after")
    def proposal_must_be_internally_consistent(self) -> AgentDecision:
        if self.kind is DecisionKind.USE_TOOL:
            if not self.tool_name or not self.tool_name.strip():
                raise ValueError("a use_tool decision must name a tool")
            if self.refusal_reason is not None:
                raise ValueError("a use_tool decision must not carry a refusal_reason")
        elif self.kind is DecisionKind.REFUSE:
            if self.refusal_reason is None:
                raise ValueError("a refuse decision must carry a refusal_reason")
            if self.tool_name:
                raise ValueError("a refuse decision must not name a tool")
        else:
            if self.tool_name:
                raise ValueError("a no_tool decision must not name a tool")
            if self.refusal_reason is not None:
                raise ValueError("a no_tool decision must not carry a refusal_reason")

        if len(self.tool_arguments) > MAX_TOOL_ARGUMENTS:
            raise ValueError("too many tool arguments")

        seen: set[str] = set()
        for argument in self.tool_arguments:
            if not argument.name.strip():
                raise ValueError("a tool argument must be named")
            if len(argument.name) > MAX_ARGUMENT_KEY_LENGTH:
                raise ValueError("tool argument name is too long")
            if len(argument.value) > MAX_ARGUMENT_VALUE_LENGTH:
                raise ValueError("tool argument value is too long")
            # A list can carry the same name twice; a mapping cannot. Rejecting
            # the duplicate is what makes `arguments` below lossless — silently
            # keeping the last one would let a proposal say two different things
            # and have only one of them audited.
            if argument.name in seen:
                raise ValueError("tool arguments must not repeat a name")
            seen.add(argument.name)
        return self

    @property
    def arguments(self) -> dict[str, str]:
        """The proposal's arguments as a mapping, for the layers above.

        The single conversion point from the wire encoding to the form policy,
        the tools and the executor already expect. Building it here is lossless
        because validation has rejected duplicate names, and it is safe because
        the mapping is derived from validated data rather than reinterpreted:
        nothing about trust changes on the way through.
        """
        return {argument.name: argument.value for argument in self.tool_arguments}


class PolicyVerdict(BaseModel):
    """The application's authorisation ruling. Server-produced, never model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: PolicyDecision
    tool_name: str | None = None
    # Read from the REGISTRY, never from the decision. None when no tool is involved.
    risk_level: ToolRiskLevel | None = None
    denial_reason: DenialReason | None = None
    detail: str = Field(
        default="",
        description="Operator-facing explanation. Names the rule, never the argument value.",
    )

    @model_validator(mode="after")
    def denial_must_state_a_reason(self) -> PolicyVerdict:
        if self.decision is PolicyDecision.DENY and self.denial_reason is None:
            raise ValueError("a denial must carry a denial_reason")
        if self.decision is not PolicyDecision.DENY and self.denial_reason is not None:
            raise ValueError("only a denial may carry a denial_reason")
        return self


class ToolCall(BaseModel):
    """One authorised invocation, as the application recorded it.

    Built by the server AFTER policy authorised it. A ToolCall existing means
    the call was permitted; it does not mean it succeeded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1)
    risk_level: ToolRiskLevel
    iteration: int = Field(ge=1, le=MAX_TOOL_ITERATIONS)


class ToolResult(BaseModel):
    """The outcome of one tool execution.

    `payload` is the tool's own typed output, serialised. `scope` is lifted to
    the top level because it is what a caller must not lose: see
    `agent.tools.EvidenceScope`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1)
    status: ToolExecutionStatus
    duration_ms: float = Field(default=0.0, ge=0.0)
    failure_category: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    """The public agent result, with everything needed to audit it.

    Carries no reasoning, no prompt, no raw tool arguments and no evidence text.
    Everything here is an identifier, an enum, a count, a duration or a
    server-built citation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    outcome: AgentOutcomeKind

    answer: str | None = Field(default=None, description="Grounded answer; None unless answered.")
    citations: list[Citation] = Field(default_factory=list)
    refusal_reason: RefusalReason | None = None

    selected_tool: str | None = None
    tool_risk_level: ToolRiskLevel | None = None
    policy_decision: PolicyDecision | None = None
    tool_execution_status: ToolExecutionStatus = ToolExecutionStatus.NOT_EXECUTED
    tool_iterations: int = Field(default=0, ge=0, le=MAX_TOOL_ITERATIONS)
    denial_reason: DenialReason | None = None

    # What a human must approve, when the agent stopped for approval. A summary
    # the server built from the registry and the validated tool input — never
    # free text the model wrote.
    approval_summary: str | None = None
    # Identifies the approval record a human must decide. Present only when the
    # turn stopped for approval; the caller uses it to submit a decision.
    approval_id: str | None = None

    prompt_version: str = Field(min_length=1)
    agent_prompt_version: str = Field(min_length=1)
    retrieval_config_version: str = Field(min_length=1)
    corpus_version: int = Field(ge=1)
    model_metadata: ModelMetadata
    latency_ms: float = Field(ge=0.0)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)

    @model_validator(mode="after")
    def outcome_invariants(self) -> AgentResponse:
        """The agent's equivalent of the answered/refused invariant.

        An answered agent response must be grounded exactly as an
        `AnswerResponse` is: non-empty text and at least one citation. Anything
        else is ungrounded output wearing an agent's clothes.
        """
        if self.outcome is AgentOutcomeKind.ANSWERED:
            if self.answer is None or not self.answer.strip():
                raise ValueError("an answered agent response must carry a non-empty answer")
            if not self.citations:
                raise ValueError("an answered agent response must carry at least one citation")
        else:
            if self.answer is not None:
                raise ValueError("only an answered agent response may carry an answer")
            if self.citations:
                raise ValueError("only an answered agent response may carry citations")

        if self.outcome is AgentOutcomeKind.APPROVAL_REQUIRED:
            if self.policy_decision is not PolicyDecision.REQUIRE_APPROVAL:
                raise ValueError("approval_required must follow a require_approval verdict")
            if self.tool_execution_status is not ToolExecutionStatus.NOT_EXECUTED:
                raise ValueError("a tool awaiting approval must not have been executed")
        if self.outcome is AgentOutcomeKind.DENIED and self.denial_reason is None:
            raise ValueError("a denied agent response must carry a denial_reason")
        if self.outcome is AgentOutcomeKind.REFUSED and self.refusal_reason is None:
            raise ValueError("a refused agent response must carry a refusal_reason")
        return self


__all__ = [
    "MAX_TOOL_ITERATIONS",
    "AgentDecision",
    "AgentOutcomeKind",
    "AgentRequest",
    "AgentResponse",
    "DecisionKind",
    "DenialReason",
    "PolicyDecision",
    "PolicyVerdict",
    "ToolArgument",
    "ToolCall",
    "ToolExecutionStatus",
    "ToolResult",
    "ToolRiskLevel",
]
