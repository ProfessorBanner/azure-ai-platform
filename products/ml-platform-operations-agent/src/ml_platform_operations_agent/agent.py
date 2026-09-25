"""The orchestrator: screen, resolve, gather, classify, compose.

THE LOOP IS FIXED, NOT PLANNED
------------------------------
`DIAGNOSTIC_TOOL_ORDER` is a constant. There is no planning step, no model
choosing what to call next, and no path by which a tool's OUTPUT can cause
another tool to run. That is the structural answer to prompt injection in
monitoring rows and runbooks (scenarios 13 and 14): injected text cannot reach
tool selection, because tool selection does not read tool output.

A dynamic plan would be more impressive and strictly worse. The bounded
question has one shape, the evidence sources for it are known in advance, and
the only thing a planner could add here is a way for retrieved text to
influence control flow.

WHY THERE IS NO MODEL IN 19.2b
-------------------------------
Nothing in this module calls a language model, and no provider is installed.
The diagnosis is composed by `_compose`, deterministically, from evidence the
tools returned. Phase 19.2d may add a model to phrase the summary more fluently;
it may not move a single decision out of `evidence.py`. `Diagnosis` is built
from typed evidence, so a later generation step can only rewrite prose that is
already anchored to citations.

DETERMINISM IS A GATE
---------------------
19.2d gates identical output for identical input at 100%. So: no clock is read
(`observed_at` is passed in), no set is iterated without sorting, evidence is
de-duplicated in first-seen order, and confidence comes from an arithmetic
ladder rather than anything learned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import perf_counter

from ml_platform_operations_agent import evidence as ev
from ml_platform_operations_agent.adapters.protocols import (
    ModelRegistrySource,
    MonitoringSource,
    RunbookSource,
    RunHistorySource,
)
from ml_platform_operations_agent.config import (
    EvidenceScope,
    monitoring_thresholds,
    parse_model_name,
    scope_for,
)
from ml_platform_operations_agent.domain import (
    AgentOutcome,
    CauseSupport,
    DegradationStatus,
    Diagnosis,
    EvidenceReference,
    LikelyCause,
    MonitoringObservation,
    RefusalReason,
    TimeWindow,
    deduplicate,
)
from ml_platform_operations_agent.errors import EvidenceSourceError, OperationsAgentError
from ml_platform_operations_agent.policy import authorise, screen_request
from ml_platform_operations_agent.tools import (
    DIAGNOSTIC_TOOL_ORDER,
    GET_DRIFT_METRICS,
    GET_MODEL_PERFORMANCE,
    GET_RECENT_MODEL_RUNS,
    INSPECT_REGISTERED_MODEL,
    SEARCH_ML_OPERATIONS_RUNBOOKS,
    ToolRegistry,
    ToolResult,
    build_registry,
    get_drift_metrics,
    get_model_performance,
    get_recent_model_runs,
    inspect_registered_model,
    search_ml_operations_runbooks,
)
from ml_platform_operations_agent.tracing import (
    TracingConfig,
    TracingMode,
    annotate_trace,
    correlation_hash,
)
from ml_platform_operations_agent.tracing import span as trace_span


@dataclass(frozen=True, slots=True)
class EvidenceSources:
    """The four adapters, injected together.

    One parameter object rather than four so that adding a fifth source in
    19.2c does not change every call site.
    """

    registry_source: ModelRegistrySource
    run_history: RunHistorySource
    monitoring: MonitoringSource
    runbooks: RunbookSource


@dataclass(frozen=True, slots=True)
class DiagnosisRequest:
    """One bounded question.

    `observed_at` is REQUIRED and is the caller's clock, not ours. A module that
    read `datetime.now()` would be untestable for determinism and would make two
    runs of the same evaluation case incomparable.
    """

    model_name: str
    window: TimeWindow
    observed_at: datetime
    question: str = "Why did this model degrade during the requested window?"
    #: Stable identifier joining a trace back to its evaluation case.
    case_id: str = ""
    #: Whether the evidence behind this request is synthetic. Carried into the
    #: trace so a stored record can never be mistaken for a live observation.
    synthetic: bool = False


class OperationsAgent:
    """The controlled core. Deterministic given its sources and its request."""

    def __init__(
        self,
        sources: EvidenceSources,
        registry: ToolRegistry | None = None,
        tracing: TracingConfig | None = None,
    ) -> None:
        self._sources = sources
        self._registry = registry or build_registry()
        # DISABLED by default. Tracing is opt-in so that a unit test, the
        # offline CLI and any future caller never initialise MLflow by
        # accident — which is what keeps "no network in the offline path" true.
        self._tracing = tracing or TracingConfig(mode=TracingMode.DISABLED)

    # -- public surface ------------------------------------------------------

    def diagnose(self, request: DiagnosisRequest) -> Diagnosis:
        """Answer the bounded question, or refuse.

        Never raises for an expected condition. A missing table, an unknown
        model and a prohibited request are all ANSWERS. Only a genuinely
        unexpected defect escapes, and `run` wraps even that.
        """
        with trace_span(self._tracing, "screen_request"):
            screening = screen_request(request.question, request.model_name)
        if not screening.allowed:
            assert screening.refusal_reason is not None
            # A refusal emits NO tool span, because no tool ran. The span tree
            # is a record of what happened, not a template of what could.
            return self._refuse(request, screening.refusal_reason, screening.detail)

        with trace_span(self._tracing, "resolve_model"):
            parsed = parse_model_name(request.model_name)
        if parsed is None:
            return self._refuse(
                request,
                RefusalReason.UNKNOWN_MODEL,
                "the model identifier is not a governed three-part name in a governed catalog",
            )

        catalog, _schema, _name = parsed
        scope = scope_for(catalog)

        results = self._gather(request, scope)

        # An unresolved model is a refusal, not an insufficient-evidence
        # diagnosis: there is no model to diagnose, which is a different thing
        # from a model whose evidence is missing.
        inspection = results[INSPECT_REGISTERED_MODEL]
        if not inspection.found:
            return self._refuse(
                request,
                RefusalReason.UNKNOWN_MODEL,
                "the model is not registered in the governed catalog",
                selected_tools=tuple(results),
            )

        return self._compose(request, scope, results)

    def run(self, request: DiagnosisRequest) -> Diagnosis:
        """`diagnose` with a failure boundary.

        A defect becomes a FAILED diagnosis carrying the failure category and
        nothing else. The exception message is deliberately discarded: it may
        quote a monitoring row, a runbook excerpt or a workspace path, and this
        value is returned to a caller and written to a log.
        """
        started = perf_counter()
        # THE ROOT SPAN. Every other span is a child of this one, and
        # `annotate_trace` needs an active trace to attach metadata to — the
        # first local run emitted "No active trace found" for exactly this
        # reason.
        with trace_span(self._tracing, "ml_platform_operations_agent"):
            return self._run_traced(request, started)

    def _run_traced(self, request: DiagnosisRequest, started: float) -> Diagnosis:
        try:
            diagnosis = self.diagnose(request)
        except OperationsAgentError as error:
            diagnosis = self._failed(request, error.category.value)
        except Exception:
            # The exception itself is NEVER traced. It can carry a monitoring
            # row, a runbook excerpt or a workspace path, and a trace is a
            # durable record read by people who were not here.
            diagnosis = self._failed(request, "internal_error")

        annotate_trace(
            self._tracing,
            case_id=request.case_id,
            selected_tools=diagnosis.selected_tools,
            degradation_status=diagnosis.degradation_status.value,
            outcome=diagnosis.outcome.value,
            refusal_reason=(diagnosis.refusal_reason.value if diagnosis.refusal_reason else "none"),
            latency_ms=round((perf_counter() - started) * 1000, 3),
            synthetic=request.synthetic,
            correlation_hash=correlation_hash(f"{request.model_name}|{request.question}"),
        )
        return diagnosis

    # -- evidence gathering --------------------------------------------------

    def _gather(
        self,
        request: DiagnosisRequest,
        scope: EvidenceScope,
    ) -> dict[str, ToolResult]:
        """Run the fixed tool sequence. Every call is authorised first.

        Authorisation is not theatre even though the sequence is a constant: it
        is what makes the read-only claim checkable at the call site rather than
        by reading the constant, and it is the assertion 19.2d's
        `read_only_compliance` scorer exercises.
        """
        results: dict[str, ToolResult] = {}

        for name in DIAGNOSTIC_TOOL_ORDER:
            # SHORT-CIRCUIT ON AN UNRESOLVED MODEL.
            #
            # If the registry says the model does not exist, every remaining
            # tool would query a workspace about a model that is not there.
            # Observed live on 2026-09-04: the unknown-model proof made two
            # needless control-plane calls before refusing. A refusal should
            # cost as little as possible, and evidence gathered about a
            # non-existent model is evidence about nothing.
            previous = results.get(INSPECT_REGISTERED_MODEL)
            if previous is not None and not previous.found:
                break

            arguments = self._arguments_for(name, request)
            verdict = authorise(name, arguments, self._registry)
            if not verdict.allowed:
                # Unreachable with the shipped registry; if it ever fires, the
                # registry and this sequence have diverged and running the call
                # anyway would be exactly the wrong recovery.
                raise EvidenceSourceError(
                    f"the diagnostic sequence names a tool the policy refuses: {verdict.detail}"
                )
            with trace_span(self._tracing, name):
                results[name] = self._invoke(name, request, scope)

        return results

    def _arguments_for(self, name: str, request: DiagnosisRequest) -> dict[str, object]:
        if name == SEARCH_ML_OPERATIONS_RUNBOOKS:
            return {"query": request.question}
        if name == INSPECT_REGISTERED_MODEL:
            return {"full_name": request.model_name}
        return {"full_name": request.model_name, "window": request.window}

    def _invoke(
        self,
        name: str,
        request: DiagnosisRequest,
        scope: EvidenceScope,
    ) -> ToolResult:
        """Dispatch. A source that malfunctions becomes an EvidenceSourceError,
        which `run` turns into a FAILED diagnosis — distinct from absence,
        which the tools return as a value."""
        try:
            if name == INSPECT_REGISTERED_MODEL:
                return inspect_registered_model(
                    self._sources.registry_source,
                    request.model_name,
                    request.window,
                    request.observed_at,
                )
            if name == GET_MODEL_PERFORMANCE:
                return get_model_performance(
                    self._sources.monitoring,
                    scope,
                    request.model_name,
                    request.window,
                    request.observed_at,
                )
            if name == GET_DRIFT_METRICS:
                return get_drift_metrics(
                    self._sources.monitoring,
                    scope,
                    request.model_name,
                    request.window,
                    request.observed_at,
                )
            if name == GET_RECENT_MODEL_RUNS:
                return get_recent_model_runs(
                    self._sources.run_history,
                    request.model_name,
                    request.window,
                    request.observed_at,
                )
            if name == SEARCH_ML_OPERATIONS_RUNBOOKS:
                return search_ml_operations_runbooks(
                    self._sources.runbooks,
                    request.question,
                    request.window,
                    request.observed_at,
                )
        except OperationsAgentError:
            raise
        except Exception as error:  # noqa: BLE001 - deliberately broad, see below
            # An adapter is third-party-shaped code. Whatever it raises becomes
            # one typed category here, without the original message: an adapter
            # exception can carry a connection string or a row of data.
            raise EvidenceSourceError(f"evidence source '{name}' failed") from error

        raise EvidenceSourceError(f"no implementation is registered for tool '{name}'")

    # -- composition ---------------------------------------------------------

    def _compose(
        self,
        request: DiagnosisRequest,
        scope: EvidenceScope,
        results: dict[str, ToolResult],
    ) -> Diagnosis:
        with trace_span(self._tracing, "evaluate_evidence"):
            observations = self._observations(results)
            thresholds = monitoring_thresholds(request.window, request.observed_at)
            status, status_limitations = ev.classify_status(
                observations, request.window, thresholds
            )

        references: list[EvidenceReference] = []
        limitations: list[str] = list(status_limitations)

        for name in DIAGNOSTIC_TOOL_ORDER:
            result = results[name]
            limitations.extend(result.limitations)
            if result.reference is not None:
                references.append(result.reference)
            elif result.absence is not None:
                limitations.append(f"{result.absence.source_identifier}: {result.absence.reason}")

        causes = self._causes(status, observations, request, results)
        assertable, demoted = ev.demote_unsupported_causes(causes)

        # Only the threshold definition and the sources actually consulted are
        # cited. A supported cause's own references are merged in so a reader
        # can get from the claim to the row without a second lookup.
        for cause in assertable:
            references.extend(cause.evidence)
            if cause.mechanism is not None:
                references.append(cause.mechanism)
        if status is DegradationStatus.DEGRADED:
            references.append(thresholds[0].defined_in)

        unique = deduplicate(tuple(references))
        resolved_version = self._resolved_version(results, observations)

        with trace_span(self._tracing, "compose_diagnosis"):
            diagnosis = Diagnosis(
                requested_model=request.model_name,
                resolved_model=str(results[INSPECT_REGISTERED_MODEL].data.get("full_name") or None),
                resolved_version=resolved_version,
                time_window=request.window,
                degradation_status=status,
                summary=self._summary(status, request, assertable, limitations),
                likely_causes=assertable,
                supporting_evidence=unique,
                confidence=ev.confidence_for(status, assertable, unique),
                recommended_investigations=tuple(dict.fromkeys(demoted)),
                limitations=tuple(dict.fromkeys(limitations)),
                # The tools that ACTUALLY ran, not the constant. With the
                # unresolved-model short-circuit these can differ, and a trace
                # claiming a tool ran when it did not would be worse than no
                # trace at all.
                selected_tools=tuple(results),
                outcome=AgentOutcome.DIAGNOSED,
            )
        return diagnosis

    def _observations(self, results: dict[str, ToolResult]) -> tuple[MonitoringObservation, ...]:
        """Rebuild typed observations from the performance tool's envelope.

        Read from ONE tool rather than merged across the two monitoring tools:
        both read the same table, and merging would double every row and inflate
        the coverage check into thinking the window is twice as observed.
        """
        source = results[GET_MODEL_PERFORMANCE]
        if not source.found:
            return ()
        rows = source.data.get("observations", [])
        if not isinstance(rows, list):
            return ()
        rebuilt: list[MonitoringObservation] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                rebuilt.append(
                    MonitoringObservation(
                        observed_at=datetime.fromisoformat(str(row["observed_at"])),
                        model_version=int(row["model_version"]),
                        status=str(row["status"]),
                        rmse=_optional_float(row.get("rmse")),
                        r2=_optional_float(row.get("r2")),
                        drift_score=_optional_float(row.get("drift_score")),
                        null_rate=_optional_float(row.get("null_rate")),
                        reasons=str(row.get("reasons", "")),
                    )
                )
            except (KeyError, ValueError, TypeError):
                # Malformed evidence is DISCARDED, never repaired. A row we
                # cannot parse is a row we cannot cite.
                continue
        return tuple(rebuilt)

    def _resolved_version(
        self,
        results: dict[str, ToolResult],
        observations: tuple[MonitoringObservation, ...],
    ) -> int | None:
        """The version the diagnosis is about.

        Taken from the MONITORING ROWS when they exist, because those record
        which version was actually scoring at the time. The current `champion`
        alias is used only when there is no monitoring evidence, and then it
        describes the present, not the window — which the limitation on the
        registry reference already says.
        """
        if observations:
            return observations[-1].model_version

        inspection = results[INSPECT_REGISTERED_MODEL]
        aliases = inspection.data.get("aliases", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, dict) and str(alias.get("normalised", "")) == "champion":
                    version = alias.get("version")
                    return int(version) if isinstance(version, int) else None
        return None

    def _causes(
        self,
        status: DegradationStatus,
        observations: tuple[MonitoringObservation, ...],
        request: DiagnosisRequest,
        results: dict[str, ToolResult],
    ) -> list[LikelyCause]:
        """Propose causes for a degraded verdict. Empty otherwise.

        A cause becomes SUPPORTED only when the drift evidence exists AND the
        runbook supplies the mechanism. Without the runbook hit the same
        observation is CORRELATION_ONLY — drift and a metric breach in the same
        window is co-movement until something independent says drift causes
        breaches.
        """
        if status is not DegradationStatus.DEGRADED:
            return []

        thresholds_typed = monitoring_thresholds(request.window, request.observed_at)
        breaches = ev.breaching_observations(observations, request.window, thresholds_typed)
        if not breaches:
            return []

        performance = results[GET_MODEL_PERFORMANCE]
        drift = results[GET_DRIFT_METRICS]
        runbook = results[SEARCH_ML_OPERATIONS_RUNBOOKS]

        breached_metrics = sorted({threshold.metric for _row, threshold, _v in breaches})
        causes: list[LikelyCause] = []

        drift_breached = "drift_score" in breached_metrics
        performance_breached = bool({"rmse", "r2"} & set(breached_metrics))

        if drift_breached and performance_breached:
            statement = (
                "Feature drift crossed its version-controlled threshold in the same window "
                "as the live performance breach."
            )
            if runbook.found and drift.reference is not None:
                causes.append(
                    LikelyCause(
                        statement=statement,
                        support=CauseSupport.SUPPORTED,
                        evidence=(drift.reference,)
                        + ((performance.reference,) if performance.reference else ()),
                        mechanism=runbook.reference,
                    )
                )
            else:
                causes.append(
                    LikelyCause(statement=statement, support=CauseSupport.CORRELATION_ONLY)
                )

        versions = {row.model_version for row in observations}
        if len(versions) > 1 and performance_breached:
            # A version change inside the window is a genuine candidate, but
            # nothing here establishes the new version CAUSED the breach — that
            # needs a per-version comparison this evidence does not contain.
            causes.append(
                LikelyCause(
                    statement=(
                        f"The serving model version changed within the window "
                        f"({min(versions)} to {max(versions)}) alongside the performance breach."
                    ),
                    support=CauseSupport.CORRELATION_ONLY,
                )
            )

        if performance_breached and not drift_breached:
            causes.append(
                LikelyCause(
                    statement=(
                        "Live performance breached its threshold with no accompanying feature "
                        "drift, which points at the label or scoring path rather than the "
                        "input distribution."
                    ),
                    support=CauseSupport.CORRELATION_ONLY,
                )
            )

        return causes

    # -- rendering -----------------------------------------------------------

    def _summary(
        self,
        status: DegradationStatus,
        request: DiagnosisRequest,
        causes: tuple[LikelyCause, ...],
        limitations: list[str],
    ) -> str:
        days = round(request.window.duration_days, 1)
        model = request.model_name

        if status is DegradationStatus.DEGRADED:
            head = (
                f"{model} shows degradation in the {days}-day window: at least one monitored "
                f"metric crossed its version-controlled threshold."
            )
            if causes:
                return head + f" {len(causes)} cause(s) are supported by cited evidence."
            return head + " No cause is supported by the available evidence."

        if status is DegradationStatus.NOT_DEGRADED:
            return (
                f"{model} shows no degradation in the {days}-day window: monitoring history "
                f"covers the window and no monitored metric crossed its threshold."
            )

        reason = limitations[0] if limitations else "the required evidence is not available"
        return (
            f"There is not enough evidence to say whether {model} degraded in the "
            f"{days}-day window: {reason}. No degradation is asserted and none is ruled out."
        )

    def _refuse(
        self,
        request: DiagnosisRequest,
        reason: RefusalReason,
        detail: str,
        selected_tools: tuple[str, ...] = (),
    ) -> Diagnosis:
        return Diagnosis(
            requested_model=request.model_name,
            resolved_model=None,
            resolved_version=None,
            time_window=request.window,
            degradation_status=DegradationStatus.INSUFFICIENT_EVIDENCE,
            summary=f"This request was not carried out: {detail}.",
            confidence=0.0,
            limitations=(detail,),
            selected_tools=selected_tools,
            outcome=AgentOutcome.REFUSED,
            refusal_reason=reason,
        )

    def _failed(self, request: DiagnosisRequest, category: str) -> Diagnosis:
        return Diagnosis(
            requested_model=request.model_name,
            resolved_model=None,
            resolved_version=None,
            time_window=request.window,
            degradation_status=DegradationStatus.INSUFFICIENT_EVIDENCE,
            summary=(
                "The request could not be completed because an evidence source failed. "
                f"Failure category: {category}."
            ),
            confidence=0.0,
            limitations=(f"evidence source failure ({category})",),
            outcome=AgentOutcome.FAILED,
        )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


__all__ = ["DiagnosisRequest", "EvidenceSources", "OperationsAgent"]
