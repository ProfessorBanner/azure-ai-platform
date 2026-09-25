"""The bounded tool registry and the five read-only tools.

WHAT A TOOL IS HERE
-------------------
A named, registered, argument-validated call onto an evidence-source protocol,
returning an envelope: `source_type`, `source_identifier`, `observed_at`,
`query_window`, `data`, `limitations`. The envelope is not decoration — a
result without a resolvable identifier cannot be cited, and 19.2d gates
identifier validity at 100%.

THE REGISTRY IS BUILT IN CODE AND FROZEN
-----------------------------------------
Nothing at request time can add a tool, rename one, or change its risk. A call
naming an unregistered tool has nowhere to resolve to, which is why an unknown
tool is a denial rather than an attempt at a near match.

EVERY TOOL IS READ_ONLY, AND THAT IS CHECKED
---------------------------------------------
`build_registry` asserts it. Phase 19.2 exposes no state-changing operation at
all: there is no retrain tool, no alias tool, no job tool and no SQL tool. The
agent cannot perform those actions because the code to perform them does not
exist in this package — which is a stronger claim than a policy that declines
to authorise them.

ARGUMENTS ARE VALIDATED AGAINST A DECLARED SHAPE
-------------------------------------------------
`validate_arguments` rejects unknown keys, missing required keys and wrong
types. Unknown keys are rejected rather than ignored so that a caller trying to
smuggle `{"sql": ...}` or `{"path": ...}` past a tool gets a denial instead of
silent success with the extra argument dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ml_platform_operations_agent.adapters.protocols import (
    ModelRegistrySource,
    MonitoringSource,
    RunbookSource,
    RunHistorySource,
)
from ml_platform_operations_agent.config import RUNBOOK_DOCUMENTS, EvidenceScope
from ml_platform_operations_agent.domain import (
    EvidenceAbsence,
    EvidenceReference,
    SourceType,
    TimeWindow,
    ToolRiskLevel,
)
from ml_platform_operations_agent.errors import ConfigurationError
from ml_platform_operations_agent.policy import redact, scan_untrusted_text

INSPECT_REGISTERED_MODEL = "inspect_registered_model"
GET_RECENT_MODEL_RUNS = "get_recent_model_runs"
GET_MODEL_PERFORMANCE = "get_model_performance"
GET_DRIFT_METRICS = "get_drift_metrics"
SEARCH_ML_OPERATIONS_RUNBOOKS = "search_ml_operations_runbooks"

#: The tool sequence the agent runs, in order. A FIXED list, not a plan the
#: model or a tool result can extend — see agent.py.
DIAGNOSTIC_TOOL_ORDER: tuple[str, ...] = (
    INSPECT_REGISTERED_MODEL,
    GET_MODEL_PERFORMANCE,
    GET_DRIFT_METRICS,
    GET_RECENT_MODEL_RUNS,
    SEARCH_ML_OPERATIONS_RUNBOOKS,
)

#: Hard ceiling on tool iterations. The agent runs a fixed sequence so this is
#: never reached in practice; it exists so that a future change which makes the
#: sequence dynamic cannot loop indefinitely without tripping a declared bound.
MAX_TOOL_ITERATIONS = 8


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """One argument, as the tool declares it."""

    name: str
    kind: type
    required: bool = True


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The envelope every tool returns.

    `reference` is None only when the tool found nothing to cite — in which case
    `absence` explains what was missing. Exactly one of the two is set, which
    `__post_init__` enforces: a result that was neither evidence nor a stated
    absence would be a silent hole in the diagnosis.
    """

    tool_name: str
    data: Mapping[str, Any]
    reference: EvidenceReference | None = None
    absence: EvidenceAbsence | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.reference is None) == (self.absence is None):
            raise ConfigurationError(
                "a tool result must carry either an evidence reference or a stated absence"
            )

    @property
    def found(self) -> bool:
        return self.reference is not None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Registry metadata plus the callable.

    Risk lives here and nowhere else. A caller's opinion about a tool's risk is
    never consulted.
    """

    name: str
    risk: ToolRiskLevel
    description: str
    arguments: tuple[ArgumentSpec, ...]
    allowed: bool = True

    def validate_arguments(self, arguments: Mapping[str, object]) -> str | None:
        """None when valid; otherwise the rule that was broken.

        Never quotes a value — the message is logged and arguments may be
        attacker-influenced.
        """
        declared = {spec.name: spec for spec in self.arguments}

        unknown = sorted(set(arguments) - set(declared))
        if unknown:
            return f"unexpected argument(s): {', '.join(unknown)}"

        for spec in self.arguments:
            if spec.name not in arguments:
                if spec.required:
                    return f"missing required argument: {spec.name}"
                continue
            value = arguments[spec.name]
            if not isinstance(value, spec.kind):
                return f"argument '{spec.name}' has the wrong type"

        return None


@dataclass(frozen=True)
class ToolRegistry:
    """Immutable catalogue of what may be called."""

    definitions: Mapping[str, ToolDefinition] = field(default_factory=dict)

    def get(self, name: str) -> ToolDefinition | None:
        return self.definitions.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.definitions))

    @property
    def state_changing_tools(self) -> tuple[str, ...]:
        """Always empty in Phase 19.2. Asserted by tests rather than assumed."""
        return tuple(
            sorted(
                name
                for name, definition in self.definitions.items()
                if definition.risk is not ToolRiskLevel.READ_ONLY
            )
        )


_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name=INSPECT_REGISTERED_MODEL,
        risk=ToolRiskLevel.READ_ONLY,
        description="Resolve a governed model name to its registry facts and current aliases.",
        arguments=(ArgumentSpec("full_name", str),),
    ),
    ToolDefinition(
        name=GET_RECENT_MODEL_RUNS,
        risk=ToolRiskLevel.READ_ONLY,
        description="List MLflow training runs for the model inside the window.",
        arguments=(ArgumentSpec("full_name", str), ArgumentSpec("window", TimeWindow)),
    ),
    ToolDefinition(
        name=GET_MODEL_PERFORMANCE,
        risk=ToolRiskLevel.READ_ONLY,
        description="Read live performance metrics from monitoring history.",
        arguments=(ArgumentSpec("full_name", str), ArgumentSpec("window", TimeWindow)),
    ),
    ToolDefinition(
        name=GET_DRIFT_METRICS,
        risk=ToolRiskLevel.READ_ONLY,
        description="Read feature-drift scores from monitoring history.",
        arguments=(ArgumentSpec("full_name", str), ArgumentSpec("window", TimeWindow)),
    ),
    ToolDefinition(
        name=SEARCH_ML_OPERATIONS_RUNBOOKS,
        risk=ToolRiskLevel.READ_ONLY,
        description="Search the approved repository runbooks for an operational mechanism.",
        arguments=(ArgumentSpec("query", str),),
    ),
)


def build_registry() -> ToolRegistry:
    """Build the frozen registry, asserting the read-only invariant."""
    registry = ToolRegistry(definitions={d.name: d for d in _DEFINITIONS})
    if registry.state_changing_tools:
        raise ConfigurationError(
            "Phase 19.2 must register no state-changing tool; the registry is misconfigured"
        )
    return registry


# --- The tool implementations ----------------------------------------------
#
# Each takes its source as a parameter. No tool constructs a client, reads an
# environment variable or opens a connection: that is what keeps them testable
# and what keeps the offline claim true.


def inspect_registered_model(
    source: ModelRegistrySource,
    full_name: str,
    window: TimeWindow,
    observed_at: datetime,
) -> ToolResult:
    """Resolve a model. Absence here means "not registered"."""
    resolved = source.resolve_model(full_name)

    if isinstance(resolved, EvidenceAbsence):
        return ToolResult(
            tool_name=INSPECT_REGISTERED_MODEL,
            data={"resolved": False},
            absence=resolved,
        )

    # Aliases are reported with BOTH forms. Unity Catalog lower-cases them while
    # Phase 15 code writes `Champion`; showing only one would make an operator
    # comparing the two think something had changed.
    aliases = [
        {"name": binding.name, "normalised": binding.name.casefold(), "version": binding.version}
        for binding in resolved.aliases
    ]

    return ToolResult(
        tool_name=INSPECT_REGISTERED_MODEL,
        data={
            "resolved": True,
            "full_name": resolved.full_name,
            "aliases": aliases,
            "version_count": len(resolved.versions),
            "versions": [
                {
                    "version": v.version,
                    "created_at": v.created_at.isoformat(),
                    "tags_available": v.tags_available,
                    "tag_count": len(v.tags),
                    "metrics": dict(v.metrics),
                }
                for v in resolved.versions
            ],
        },
        reference=EvidenceReference(
            source_type=SourceType.UNITY_CATALOG_MODEL,
            source_identifier=resolved.full_name,
            observed_at=observed_at,
            query_window=window,
            relevant_fields=("aliases", "versions", "tags"),
            limitations=(
                "Alias bindings are CURRENT state only. Unity Catalog exposes no alias "
                "history, so which version held an alias at an earlier time cannot be "
                "determined from this source.",
            ),
        ),
    )


def get_recent_model_runs(
    source: RunHistorySource,
    full_name: str,
    window: TimeWindow,
    observed_at: datetime,
    limit: int = 20,
) -> ToolResult:
    """Training runs in the window. Provenance, not a degradation signal."""
    runs = source.recent_runs(full_name, window, limit=limit)

    if isinstance(runs, EvidenceAbsence):
        return ToolResult(tool_name=GET_RECENT_MODEL_RUNS, data={"runs": []}, absence=runs)

    distinct_metrics = {
        tuple(sorted((key, round(value, 6)) for key, value in metrics.items()))
        for _run_id, metrics in runs
    }
    limitations: list[str] = [
        "Training-time metrics describe a fit on held-out data, not live behaviour. "
        "They cannot establish that a deployed model degraded.",
    ]
    if len(runs) > 1 and len(distinct_metrics) == 1:
        # The live DEV situation. Saying so explicitly is what stops a reader
        # concluding the model is stable from evidence that could not show
        # instability either.
        limitations.append(
            "Every run in this window records identical metrics, so this source cannot "
            "discriminate between a healthy and a degraded model."
        )

    first_run = runs[0][0] if runs else None
    if first_run is None:
        return ToolResult(
            tool_name=GET_RECENT_MODEL_RUNS,
            data={"runs": []},
            absence=EvidenceAbsence(
                source_type=SourceType.UNITY_CATALOG_MODEL,
                source_identifier=full_name,
                reason="no training runs fall inside the requested window",
                query_window=window,
            ),
        )

    return ToolResult(
        tool_name=GET_RECENT_MODEL_RUNS,
        data={
            "run_count": len(runs),
            "runs": [{"run_id": rid, "metrics": dict(m)} for rid, m in runs],
            "distinct_metric_sets": len(distinct_metrics),
        },
        reference=EvidenceReference(
            source_type=SourceType.MLFLOW_RUN,
            source_identifier=first_run,
            observed_at=observed_at,
            query_window=window,
            relevant_fields=("metrics.rmse", "metrics.r2"),
            limitations=tuple(limitations),
        ),
        limitations=tuple(limitations),
    )


def _monitoring_result(
    tool_name: str,
    source: MonitoringSource,
    scope: EvidenceScope,
    full_name: str,
    window: TimeWindow,
    observed_at: datetime,
    fields: tuple[str, ...],
) -> ToolResult:
    """Shared body for the two monitoring tools.

    They differ only in which columns they claim to have read, and sharing the
    body means the absence path — the common one in DEV — is written once.
    """
    table = scope.table("monitoring_history")
    history = source.monitoring_history(full_name, window)

    if isinstance(history, EvidenceAbsence):
        return ToolResult(tool_name=tool_name, data={"observations": []}, absence=history)

    if not history:
        return ToolResult(
            tool_name=tool_name,
            data={"observations": []},
            absence=EvidenceAbsence(
                source_type=SourceType.UNITY_CATALOG_TABLE,
                source_identifier=table,
                reason="the monitoring table exists but holds no rows in the requested window",
                query_window=window,
            ),
        )

    limitations: list[str] = []
    for row in history:
        limitations.extend(scan_untrusted_text(row.reasons))

    return ToolResult(
        tool_name=tool_name,
        data={
            "table": table,
            "observations": [
                {
                    "observed_at": row.observed_at.isoformat(),
                    "model_version": row.model_version,
                    "status": row.status,
                    "rmse": row.rmse,
                    "r2": row.r2,
                    "drift_score": row.drift_score,
                    "null_rate": row.null_rate,
                    "reasons": redact(row.reasons),
                }
                for row in history
            ],
        },
        reference=EvidenceReference(
            source_type=SourceType.UNITY_CATALOG_TABLE,
            source_identifier=table,
            observed_at=observed_at,
            query_window=window,
            relevant_fields=fields,
        ),
        # De-duplicated: one injection annotation is a warning, twenty is noise
        # that buries the rest of the limitations.
        limitations=tuple(dict.fromkeys(limitations)),
    )


def get_model_performance(
    source: MonitoringSource,
    scope: EvidenceScope,
    full_name: str,
    window: TimeWindow,
    observed_at: datetime,
) -> ToolResult:
    return _monitoring_result(
        GET_MODEL_PERFORMANCE,
        source,
        scope,
        full_name,
        window,
        observed_at,
        ("observed_at", "model_version", "rmse", "r2", "status"),
    )


def get_drift_metrics(
    source: MonitoringSource,
    scope: EvidenceScope,
    full_name: str,
    window: TimeWindow,
    observed_at: datetime,
) -> ToolResult:
    return _monitoring_result(
        GET_DRIFT_METRICS,
        source,
        scope,
        full_name,
        window,
        observed_at,
        ("observed_at", "model_version", "drift_score", "null_rate", "reasons"),
    )


def search_ml_operations_runbooks(
    source: RunbookSource,
    query: str,
    window: TimeWindow,
    observed_at: datetime,
    limit: int = 3,
) -> ToolResult:
    """Search approved runbooks.

    The excerpt returned is EVIDENCE. It is scanned for instruction-shaped text
    and annotated, never obeyed and never quoted back.
    """
    hits = source.search(query, limit=limit)

    if isinstance(hits, EvidenceAbsence):
        return ToolResult(
            tool_name=SEARCH_ML_OPERATIONS_RUNBOOKS, data={"sections": []}, absence=hits
        )

    if not hits:
        return ToolResult(
            tool_name=SEARCH_ML_OPERATIONS_RUNBOOKS,
            data={"sections": []},
            absence=EvidenceAbsence(
                source_type=SourceType.REPOSITORY_DOCUMENT,
                source_identifier=RUNBOOK_DOCUMENTS[0],
                reason="no approved runbook section matches this query",
                query_window=window,
            ),
        )

    limitations: list[str] = []
    for _path, _anchor, excerpt in hits:
        limitations.extend(scan_untrusted_text(excerpt))

    path, anchor, _excerpt = hits[0]
    return ToolResult(
        tool_name=SEARCH_ML_OPERATIONS_RUNBOOKS,
        data={
            "sections": [
                {"document": p, "anchor": a, "excerpt": redact(text, 300)} for p, a, text in hits
            ]
        },
        reference=EvidenceReference(
            source_type=SourceType.REPOSITORY_DOCUMENT,
            source_identifier=f"{path}#{anchor}" if anchor else path,
            observed_at=observed_at,
            query_window=window,
            relevant_fields=("section",),
        ),
        limitations=tuple(dict.fromkeys(limitations)),
    )


def approved_runbook_paths() -> Sequence[str]:
    return RUNBOOK_DOCUMENTS


__all__ = [
    "DIAGNOSTIC_TOOL_ORDER",
    "GET_DRIFT_METRICS",
    "GET_MODEL_PERFORMANCE",
    "GET_RECENT_MODEL_RUNS",
    "INSPECT_REGISTERED_MODEL",
    "MAX_TOOL_ITERATIONS",
    "SEARCH_ML_OPERATIONS_RUNBOOKS",
    "ArgumentSpec",
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
    "get_drift_metrics",
    "get_model_performance",
    "get_recent_model_runs",
    "inspect_registered_model",
    "search_ml_operations_runbooks",
]
