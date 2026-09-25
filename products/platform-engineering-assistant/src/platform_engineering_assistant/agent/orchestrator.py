"""The controlled agent loop: explicit, bounded, and readable end to end.

    request
      -> model proposes an AgentDecision        (untrusted)
      -> schema validation                      (closed model, fail closed)
      -> deterministic policy                   (registry-owned risk)
           |- no_tool / refuse -> existing grounded answering path
           |- ALLOW            -> execute tool -> ground an answer on its evidence
           |- REQUIRE_APPROVAL -> STOP, return approval_required, execute NOTHING
           |- DENY             -> safe controlled outcome
      -> AgentResponse + telemetry

WHY A DIRECT LOOP AND NOT A FRAMEWORK
-------------------------------------
Every step above is a function call whose inputs and outputs are visible in one
file. That is what makes the authorisation guarantee reviewable: a reader can
confirm, by reading, that no path reaches `tool.run` without passing
`authorise`. A framework would hide precisely that step behind its own control
flow, and the control flow IS the security property here.

GROUNDING IS UNCHANGED
----------------------
Answers are produced by the existing pipeline: the existing prompt, the existing
`build_context`, the existing `enforce_grounding`, and citations built by the
server from trusted chunk metadata. A tool contributes WHICH chunks are
considered — never what may be said about them, and never a citation.

THE LOOP IS BOUNDED BY CONSTRUCTION
-----------------------------------
`MAX_TOOL_ITERATIONS` is a hard ceiling, not a suggestion, and the loop is a
`for` over a range rather than a `while` with a break. An unbounded agent loop
against a metered model is a cost incident waiting for one bad decision.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from platform_engineering_assistant.agent.approval import ApprovalService, ApprovalStatus
from platform_engineering_assistant.agent.domain import (
    MAX_TOOL_ITERATIONS,
    AgentDecision,
    AgentOutcomeKind,
    AgentRequest,
    AgentResponse,
    DecisionKind,
    DenialReason,
    PolicyDecision,
    PolicyVerdict,
    ToolExecutionStatus,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.execution import (
    ExecutionStatus,
    ToolExecutor,
    ToolFailureCategory,
    idempotency_key_for,
)
from platform_engineering_assistant.agent.policy import authorise
from platform_engineering_assistant.agent.protocol import (
    AgentDecisionProvider,
    DecisionRequest,
)
from platform_engineering_assistant.agent.registry import ToolDefinition, ToolRegistry
from platform_engineering_assistant.agent.telemetry import AgentTelemetry
from platform_engineering_assistant.agent.tools import (
    ComponentLookupInput,
    LookupPlatformComponentTool,
    ProposeChangeInput,
    ProposeChangeRequestTool,
    SearchDocsInput,
    SearchPlatformDocsTool,
    ToolError,
    ToolInput,
)
from platform_engineering_assistant.agent.trajectory import (
    AgentTrajectory,
    TrajectoryEventKind,
    TrajectoryRecorder,
    TrajectorySink,
)
from platform_engineering_assistant.answering import AnsweringService
from platform_engineering_assistant.context import build_context
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.domain import (
    AnswerRequest,
    AnswerStatus,
    ModelMetadata,
    RefusalReason,
    TokenUsage,
)
from platform_engineering_assistant.errors import AssistantError
from platform_engineering_assistant.generation.protocol import GenerationRequest
from platform_engineering_assistant.grounding import enforce_grounding
from platform_engineering_assistant.prompts import LoadedPrompt
from platform_engineering_assistant.retrieval.index import RetrievalResult


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """The public response plus the telemetry describing how it was produced.

    `proposed_arguments` is SERVER-SIDE ONLY and never reaches the response: the
    durable workflow record needs the exact arguments in order to resume an
    approved action, while the HTTP response deliberately does not expose them.
    """

    response: AgentResponse
    telemetry: AgentTelemetry
    proposed_arguments: dict[str, str] = field(default_factory=dict)
    # Phase 18.5. The ordered audit record of this turn. Present on every turn,
    # empty only when nothing was recorded, so a caller never has to ask whether
    # observability happened to be configured before it can inspect a run.
    trajectory: AgentTrajectory | None = None
    # The still-open recorder behind that trajectory. The workflow engine
    # appends its state transitions to it, so a turn that was persisted has ONE
    # audit record rather than two that must be joined by a reader.
    recorder: TrajectoryRecorder | None = None


def render_catalogue(registry: ToolRegistry) -> str:
    """The tool catalogue as the model is shown it.

    Name, purpose and the argument contract — derived from each tool's own input
    model, never hand-written here. Risk is still absent: the model proposes,
    the registry classifies.

    Rendered compactly because this text is sent on every decision call and is
    billed each time.
    """
    lines: list[str] = []
    for entry in registry.catalogue():
        lines.append(f"- {entry.name}: {entry.description}")
        if not entry.arguments:
            lines.append("  arguments: none")
            continue
        lines.append("  arguments:")
        for argument in entry.arguments:
            requirement = "required" if argument.required else "optional"
            detail = f" — {argument.description}" if argument.description else ""
            lines.append(f"    {argument.name}: {argument.type}, {requirement}{detail}")
    return "\n".join(lines)


class AgentService:
    """Immutable, fully-configured agent pipeline."""

    def __init__(
        self,
        answering: AnsweringService,
        registry: ToolRegistry,
        decider: AgentDecisionProvider,
        agent_prompt: LoadedPrompt,
        max_iterations: int = MAX_TOOL_ITERATIONS,
        executor: ToolExecutor | None = None,
        approvals: ApprovalService | None = None,
        trajectory_sink: TrajectorySink | None = None,
    ) -> None:
        self._answering = answering
        self._registry = registry
        self._decider = decider
        self._agent_prompt = agent_prompt
        self._max_iterations = min(max_iterations, MAX_TOOL_ITERATIONS)
        # Phase 18.2: execution reliability lives behind this seam. The
        # orchestrator still owns the loop; the executor owns timeouts, bounded
        # retry and idempotency, and authorises nothing.
        self._executor = executor or ToolExecutor()
        # Phase 18.3: approval is a control, not a convention. The service
        # records what was asked for and rules on whether it may execute; it
        # never executes anything itself.
        self._approvals = approvals or ApprovalService()
        # Phase 18.5: where trajectory events go. Defaults to nothing, so a
        # deployment opts in to a sink rather than opting out of one, and a
        # failing sink can never fail a turn.
        self._trajectory_sink = trajectory_sink

    @property
    def trajectory_sink(self) -> TrajectorySink | None:
        return self._trajectory_sink

    @property
    def answering_service(self) -> AnsweringService:
        """The answering pipeline this agent grounds on. One per deployment."""
        return self._answering

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def agent_prompt(self) -> LoadedPrompt:
        return self._agent_prompt

    @property
    def executor(self) -> ToolExecutor:
        return self._executor

    @property
    def approvals(self) -> ApprovalService:
        return self._approvals

    # --- the loop -----------------------------------------------------------

    def run(self, request: AgentRequest, request_id: str | None = None) -> AgentTurn:
        """Run one bounded agent turn.

        Raises:
            AssistantError: only for provider and configuration failures. A
                denial, a refusal and an approval requirement are all normal
                outcomes and never raise.
        """
        correlation_id = request_id or f"agt-{uuid.uuid4().hex[:16]}"
        started = time.perf_counter()

        recorder = TrajectoryRecorder(
            trajectory_id=correlation_id, sink=self._trajectory_sink, started=started
        )
        base = _TurnState(
            correlation_id=correlation_id,
            question_chars=len(request.question),
            recorder=recorder,
        )
        recorder.record(TrajectoryEventKind.REQUEST_RECEIVED, question_chars=len(request.question))

        # --- 1. the model proposes -----------------------------------------
        decision_started = time.perf_counter()
        try:
            outcome = self._decider.decide(
                DecisionRequest(
                    system_prompt=self._agent_prompt.text,
                    question=request.question,
                    tool_catalogue=render_catalogue(self._registry),
                )
            )
        except AssistantError:
            # Provider failures are the API layer's to map. The agent does not
            # invent an answer when it could not obtain a proposal.
            raise
        base.decision_ms = (time.perf_counter() - decision_started) * 1000.0
        base.record_provider(outcome)

        decision = outcome.decision
        base.decision_kind = decision.kind
        base.claimed_risk_level = decision.claimed_risk_level
        base.tool_argument_count = len(decision.tool_arguments)
        base.proposed_arguments = decision.arguments

        # The proposal, as metadata. The tool NAME is recorded because it is a
        # registry identifier rather than content; the arguments are not.
        recorder.record(
            TrajectoryEventKind.MODEL_DECISION,
            decision_kind=decision.kind,
            claimed_risk_level=decision.claimed_risk_level,
            argument_count=len(decision.tool_arguments),
            duration_ms=base.decision_ms,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            total_tokens=outcome.total_tokens,
        )

        # --- 2. deterministic policy ----------------------------------------
        verdict = authorise(decision, self._registry)
        base.policy_decision = verdict.decision
        base.denial_reason = verdict.denial_reason
        base.selected_tool = verdict.tool_name
        base.tool_risk_level = verdict.risk_level
        # Recorded, never acted on. Risk came from the registry above.
        base.risk_claim_mismatch = (
            decision.claimed_risk_level is not None
            and verdict.risk_level is not None
            and decision.claimed_risk_level is not verdict.risk_level
        )
        recorder.record(
            TrajectoryEventKind.POLICY_VERDICT,
            policy_decision=verdict.decision,
            tool_name=verdict.tool_name,
            tool_risk_level=verdict.risk_level,
            denial_reason=verdict.denial_reason,
            claimed_risk_level=decision.claimed_risk_level,
            risk_claim_mismatch=base.risk_claim_mismatch,
        )

        if verdict.decision is PolicyDecision.DENY:
            return self._denied(base, started)

        if verdict.decision is PolicyDecision.REQUIRE_APPROVAL:
            return self._approval_required(base, decision, verdict, started)

        if decision.kind is DecisionKind.REFUSE:
            return self._refused(
                base, decision.refusal_reason or RefusalReason.OUT_OF_SCOPE, started
            )

        if decision.kind is DecisionKind.NO_TOOL:
            return self._grounded_without_tool(base, request, started)

        return self._grounded_with_tool(base, request, verdict, decision, started)

    # --- outcome constructors ----------------------------------------------

    def _grounded_without_tool(
        self, base: _TurnState, request: AgentRequest, started: float
    ) -> AgentTurn:
        """Delegate to the existing answering pipeline, entirely unchanged."""
        answered = self._answering.answer(
            AnswerRequest(question=request.question), request_id=base.correlation_id
        )
        base.retrieved_chunk_ids = tuple(answered.telemetry.retrieved_chunk_ids)
        base.tool_iterations = 0
        return self._from_answer(base, answered.response, started)

    def _grounded_with_tool(
        self,
        base: _TurnState,
        request: AgentRequest,
        verdict: PolicyVerdict,
        decision: AgentDecision,
        started: float,
    ) -> AgentTurn:
        """Execute an authorised read-only tool, then ground an answer on it."""
        definition = self._registry.get(verdict.tool_name or "")
        assert definition is not None  # policy resolved it; unreachable otherwise

        base.tool_iterations = 1
        try:
            chunks, duration_ms, failure, attempts, call_id = self._execute(
                base, definition, decision
            )
        except ToolError as error:
            base.tool_execution_status = ToolExecutionStatus.FAILED
            base.tool_failure_category = ToolFailureCategory.PERMANENT_FAILURE
            base.tool_attempts = 1
            assert error is not None
            return self._failed(base, started)

        base.tool_duration_ms = duration_ms
        base.tool_attempts = attempts
        base.tool_call_id = call_id
        if failure is not None:
            base.tool_execution_status = ToolExecutionStatus.FAILED
            base.tool_failure_category = failure
            return self._failed(base, started)

        base.tool_execution_status = ToolExecutionStatus.SUCCEEDED
        base.retrieved_chunk_ids = tuple(chunk.chunk_id for chunk in chunks)

        if not chunks:
            # The tool ran and found nothing. That is evidence of absence, not a
            # licence to answer from elsewhere.
            return self._refused(base, RefusalReason.INSUFFICIENT_EVIDENCE, started)

        # --- second iteration: ground an answer on exactly those chunks -----
        if self._max_iterations < 2:
            return self._refused(base, RefusalReason.INSUFFICIENT_EVIDENCE, started)
        base.tool_iterations = 2

        answer_started = time.perf_counter()
        response = self._ground_on(base, request, chunks)
        base.answer_ms = (time.perf_counter() - answer_started) * 1000.0
        return self._from_answer(base, response, started)

    def _execute(
        self, base: _TurnState, definition: ToolDefinition, decision: AgentDecision
    ) -> tuple[list[Chunk], float, ToolFailureCategory | None, int, str]:
        """Run the authorised tool through the reliability executor.

        A state-changing tool never reaches here: policy returned
        REQUIRE_APPROVAL and the loop stopped. The explicit rejection below is
        defence in depth, so a future refactor that reordered the checks fails
        loudly rather than silently executing.
        """
        if definition.risk is not ToolRiskLevel.READ_ONLY:
            raise ToolError("Only read-only tools may be executed without approval.")

        tool = definition.tool
        if isinstance(tool, SearchPlatformDocsTool):
            payload: ToolInput = SearchDocsInput.model_validate(decision.arguments)
        elif isinstance(tool, LookupPlatformComponentTool):
            payload = ComponentLookupInput.model_validate(decision.arguments)
        else:
            raise ToolError("The authorised tool has no execution binding.")

        # The call is recorded BEFORE it runs, and with the id the executor
        # will use. An audit trail that only records calls that returned cannot
        # show an action that started and never came back — which is precisely
        # the state a timeout or a crash leaves behind.
        tool_call_id = f"tc-{uuid.uuid4().hex[:16]}"
        fingerprint = idempotency_key_for(definition.name, payload)
        base.recorder.record(
            TrajectoryEventKind.TOOL_CALL,
            tool_name=definition.name,
            tool_risk_level=definition.risk,
            tool_call_id=tool_call_id,
            argument_fingerprint=fingerprint,
            argument_count=len(decision.tool_arguments),
            iteration=base.tool_iterations,
        )

        outcome = self._executor.execute(definition, payload, tool_call_id=tool_call_id)
        base.recorder.record(
            TrajectoryEventKind.TOOL_RESULT,
            tool_name=definition.name,
            tool_risk_level=definition.risk,
            tool_call_id=outcome.tool_call_id,
            argument_fingerprint=fingerprint,
            execution_status=(
                ToolExecutionStatus.SUCCEEDED
                if outcome.status is ExecutionStatus.SUCCEEDED
                else ToolExecutionStatus.FAILED
            ),
            failure_category=(
                outcome.failure_category.value if outcome.failure_category is not None else None
            ),
            attempts=outcome.attempts,
            duration_ms=outcome.total_latency_ms,
            iteration=base.tool_iterations,
        )
        if outcome.status is not ExecutionStatus.SUCCEEDED:
            return (
                [],
                outcome.total_latency_ms,
                outcome.failure_category,
                outcome.attempts,
                outcome.tool_call_id,
            )

        # Honour a tool's own scope refusal: when it reports no evidence for the
        # environment asked about, there is nothing to ground on.
        if isinstance(tool, LookupPlatformComponentTool):
            assert isinstance(payload, ComponentLookupInput)
            result = outcome.output
            found = bool(getattr(result, "found", False))
            chunks = tool.chunks_for(payload) if found else []
        else:
            assert isinstance(payload, SearchDocsInput)
            chunks = tool.chunks_for(payload)

        return chunks, outcome.total_latency_ms, None, outcome.attempts, outcome.tool_call_id

    def _ground_on(self, base: _TurnState, request: AgentRequest, chunks: list[Chunk]) -> object:
        """Produce a grounded answer over exactly the tool's chunks.

        Uses the EXISTING answering prompt, context builder and grounding policy.
        Citations are rebuilt server-side from trusted chunk metadata, so a tool
        cannot contribute a citation any more than a model can.
        """
        from platform_engineering_assistant.domain import AnswerResponse

        results = [RetrievalResult(chunk=chunk, score=0.0) for chunk in chunks]
        context = build_context(results, self._answering.generation_config.context_budget_chars)

        provider_outcome = self._answering.provider.generate(
            GenerationRequest(
                system_prompt=self._answering.prompt.text,
                context=context.text,
                question=request.question,
                max_answer_chars=self._answering.generation_config.max_answer_chars,
            )
        )
        base.record_generation(provider_outcome.telemetry)

        grounding = enforce_grounding(provider_outcome.draft, list(context.included))
        metadata = ModelMetadata(
            provider=provider_outcome.telemetry.provider,
            deployment=provider_outcome.telemetry.deployment,
            model=provider_outcome.telemetry.model,
        )
        usage = TokenUsage(
            input_tokens=provider_outcome.telemetry.input_tokens,
            output_tokens=provider_outcome.telemetry.output_tokens,
            total_tokens=provider_outcome.telemetry.total_tokens,
        )

        if not grounding.accepted:
            return AnswerResponse(
                status=AnswerStatus.REFUSED,
                refusal_reason=grounding.refusal_reason or RefusalReason.INSUFFICIENT_EVIDENCE,
                request_id=base.correlation_id,
                prompt_version=self._answering.prompt.version,
                retrieval_config_version=self._answering.retrieval_config.version,
                corpus_version=self._answering.corpus_version,
                model_metadata=metadata,
                latency_ms=0.0,
                token_usage=usage,
            )

        text = (provider_outcome.draft.answer or "").strip()[
            : self._answering.generation_config.max_answer_chars
        ]
        return AnswerResponse(
            status=AnswerStatus.ANSWERED,
            answer=text,
            citations=list(grounding.citations),
            request_id=base.correlation_id,
            prompt_version=self._answering.prompt.version,
            retrieval_config_version=self._answering.retrieval_config.version,
            corpus_version=self._answering.corpus_version,
            model_metadata=metadata,
            latency_ms=0.0,
            token_usage=usage,
        )

    # --- terminal states ----------------------------------------------------

    def _from_answer(self, base: _TurnState, response: object, started: float) -> AgentTurn:
        from platform_engineering_assistant.domain import AnswerResponse

        assert isinstance(response, AnswerResponse)
        if response.status is AnswerStatus.REFUSED:
            return self._refused(
                base, response.refusal_reason or RefusalReason.INSUFFICIENT_EVIDENCE, started
            )
        return self._build(
            base,
            AgentOutcomeKind.ANSWERED,
            started,
            answer=response.answer,
            citations=list(response.citations),
        )

    def _refused(self, base: _TurnState, reason: RefusalReason, started: float) -> AgentTurn:
        return self._build(base, AgentOutcomeKind.REFUSED, started, refusal_reason=reason)

    def _denied(self, base: _TurnState, started: float) -> AgentTurn:
        return self._build(base, AgentOutcomeKind.DENIED, started)

    def _failed(self, base: _TurnState, started: float) -> AgentTurn:
        return self._build(base, AgentOutcomeKind.FAILED, started)

    def _approval_required(
        self,
        base: _TurnState,
        decision: AgentDecision,
        verdict: PolicyVerdict,
        started: float,
    ) -> AgentTurn:
        """Stop. Execute nothing. Record exactly what a human is being asked to approve."""
        summary: str | None = None
        approval_id: str | None = None
        definition = self._registry.get(verdict.tool_name or "")

        if definition is not None and isinstance(definition.tool, ProposeChangeRequestTool):
            payload = ProposeChangeInput.model_validate(decision.arguments)
            summary = ProposeChangeRequestTool.summarise(payload)
            request = self._approvals.request(
                tool_name=definition.name,
                payload=payload,
                summary=summary,
                requested_by=base.correlation_id,
                context_id=base.correlation_id,
            )
            approval_id = request.approval_id
            base.approval_id = approval_id
            base.recorder.record(
                TrajectoryEventKind.APPROVAL_REQUESTED,
                tool_name=definition.name,
                tool_risk_level=definition.risk,
                approval_id=approval_id,
                approval_status=ApprovalStatus.PENDING.value,
                argument_fingerprint=request.action.argument_fingerprint,
                argument_count=len(decision.tool_arguments),
            )

        return self._build(
            base,
            AgentOutcomeKind.APPROVAL_REQUIRED,
            started,
            approval_summary=summary,
            approval_id=approval_id,
        )

    def _build(
        self,
        base: _TurnState,
        outcome: AgentOutcomeKind,
        started: float,
        *,
        answer: str | None = None,
        citations: list[object] | None = None,
        refusal_reason: RefusalReason | None = None,
        approval_summary: str | None = None,
        approval_id: str | None = None,
    ) -> AgentTurn:
        from platform_engineering_assistant.domain import Citation

        total_ms = (time.perf_counter() - started) * 1000.0
        typed_citations = [c for c in (citations or []) if isinstance(c, Citation)]

        response = AgentResponse(
            request_id=base.correlation_id,
            outcome=outcome,
            answer=answer,
            citations=typed_citations,
            refusal_reason=refusal_reason,
            selected_tool=base.selected_tool,
            tool_risk_level=base.tool_risk_level,
            policy_decision=base.policy_decision,
            tool_execution_status=base.tool_execution_status,
            tool_iterations=base.tool_iterations,
            denial_reason=base.denial_reason,
            approval_summary=approval_summary,
            approval_id=approval_id,
            prompt_version=self._answering.prompt.version,
            agent_prompt_version=self._agent_prompt.version,
            retrieval_config_version=self._answering.retrieval_config.version,
            corpus_version=self._answering.corpus_version,
            model_metadata=ModelMetadata(
                provider=base.provider or self._decider.name,
                deployment=base.deployment,
                model=base.model,
            ),
            latency_ms=total_ms,
            token_usage=TokenUsage(
                input_tokens=base.input_tokens,
                output_tokens=base.output_tokens,
                total_tokens=base.total_tokens,
            ),
        )
        telemetry = AgentTelemetry(
            request_id=base.correlation_id,
            decision_kind=base.decision_kind,
            selected_tool=base.selected_tool,
            tool_risk_level=base.tool_risk_level,
            claimed_risk_level=base.claimed_risk_level,
            risk_claim_mismatch=base.risk_claim_mismatch,
            policy_decision=base.policy_decision,
            denial_reason=base.denial_reason,
            tool_execution_status=base.tool_execution_status,
            tool_duration_ms=base.tool_duration_ms,
            tool_failure_category=base.tool_failure_category,
            tool_attempts=base.tool_attempts,
            tool_call_id=base.tool_call_id,
            tool_argument_count=base.tool_argument_count,
            tool_iterations=base.tool_iterations,
            retrieved_chunk_ids=base.retrieved_chunk_ids,
            outcome=outcome,
            refusal_reason=refusal_reason,
            citation_count=len(typed_citations),
            decision_ms=base.decision_ms,
            answer_ms=base.answer_ms,
            total_ms=total_ms,
            question_chars=base.question_chars,
            agent_prompt_version=self._agent_prompt.version,
            agent_prompt_hash=self._agent_prompt.content_hash,
            prompt_version=self._answering.prompt.version,
            retrieval_config_version=self._answering.retrieval_config.version,
            corpus_version=self._answering.corpus_version,
            provider=base.provider or self._decider.name,
            model=base.model,
            deployment=base.deployment,
            input_tokens=base.input_tokens,
            output_tokens=base.output_tokens,
            total_tokens=base.total_tokens,
        )
        base.recorder.record(
            TrajectoryEventKind.OUTCOME,
            outcome=outcome,
            refusal_reason=refusal_reason,
            citation_count=len(typed_citations),
            policy_decision=base.policy_decision,
            denial_reason=base.denial_reason,
            tool_name=base.selected_tool,
            tool_risk_level=base.tool_risk_level,
            execution_status=base.tool_execution_status,
            approval_id=base.approval_id,
            iteration=base.tool_iterations,
            duration_ms=total_ms,
            input_tokens=base.input_tokens,
            output_tokens=base.output_tokens,
            total_tokens=base.total_tokens,
        )
        trajectory = base.recorder.build(
            agent_prompt_version=self._agent_prompt.version,
            agent_prompt_hash=self._agent_prompt.content_hash,
            prompt_version=self._answering.prompt.version,
            retrieval_config_version=self._answering.retrieval_config.version,
            corpus_version=self._answering.corpus_version,
            provider=base.provider or self._decider.name,
            model=base.model,
            deployment=base.deployment,
        )
        return AgentTurn(
            response=response,
            telemetry=telemetry,
            proposed_arguments=base.proposed_arguments,
            trajectory=trajectory,
            recorder=base.recorder,
        )


@dataclass
class _TurnState:
    """Mutable accumulator for one turn. Never leaves this module."""

    correlation_id: str
    question_chars: int = 0
    # Assigned by `run`. A turn always has one; a turn without a sink still has
    # a recorder, so no instrumented code path needs a None check.
    recorder: TrajectoryRecorder = field(default_factory=lambda: TrajectoryRecorder("unbound"))

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
    approval_id: str | None = None
    proposed_arguments: dict[str, str] = field(default_factory=dict)
    tool_argument_count: int = 0
    tool_iterations: int = 0
    retrieved_chunk_ids: tuple[str, ...] = ()

    decision_ms: float = 0.0
    answer_ms: float = 0.0

    provider: str = ""
    model: str | None = None
    deployment: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def record_provider(self, outcome: object) -> None:
        from platform_engineering_assistant.agent.protocol import DecisionOutcome

        assert isinstance(outcome, DecisionOutcome)
        self.provider = ""
        self.model = outcome.model
        self.deployment = outcome.deployment
        self.input_tokens = outcome.input_tokens
        self.output_tokens = outcome.output_tokens
        self.total_tokens = outcome.total_tokens

    def record_generation(self, telemetry: object) -> None:
        """Fold the answering call's usage into the turn's totals."""
        from platform_engineering_assistant.generation.protocol import ProviderTelemetry

        assert isinstance(telemetry, ProviderTelemetry)
        self.model = telemetry.model or self.model
        self.deployment = telemetry.deployment or self.deployment
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            existing = getattr(self, name) or 0
            addition = getattr(telemetry, name) or 0
            setattr(self, name, existing + addition if (existing or addition) else None)


def build_agent_service(
    answering: AnsweringService,
    decider: AgentDecisionProvider,
    trajectory_sink: TrajectorySink | None = None,
) -> AgentService:
    """Assemble the agent from an already-built answering service.

    The approval service is built HERE with the same sink, so a human decision
    recorded through the approval routes reaches the same audit trail as the
    turn that asked for it.
    """
    from platform_engineering_assistant.agent.registry import build_registry
    from platform_engineering_assistant.prompts import load_named_prompt

    return AgentService(
        answering=answering,
        registry=build_registry(answering.index),
        decider=decider,
        agent_prompt=load_named_prompt("agent_decision_v1.md"),
        approvals=ApprovalService(trajectory_sink=trajectory_sink),
        trajectory_sink=trajectory_sink,
    )


__all__ = [
    "AgentService",
    "AgentTurn",
    "build_agent_service",
    "render_catalogue",
]
