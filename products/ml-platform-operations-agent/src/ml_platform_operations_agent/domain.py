"""The domain contracts. Everything else in this package depends on this module.

THE ONE RULE THIS MODULE ENCODES
--------------------------------
A diagnosis may only say what the evidence supports. Phase 19.2a found that the
DEV workspace has no `monitoring_history` at all, which makes
`insufficient_evidence` the ORDINARY outcome rather than an edge case. So it is
modelled first, and `degraded` is the outcome that has to earn its way past
`permits_degradation_finding`.

WHY IDENTIFIERS ARE PARSED, NOT TRUSTED
---------------------------------------
`EvidenceReference.source_identifier` is validated against the shape its
`source_type` demands: a table reference must be `catalog.schema.table`, a run
reference a 32-character hex id, a document reference a repository-relative
path. Free text is rejected.

That is not tidiness. Phase 19.2d gates evidence-identifier validity at 100%,
and a citation an operator cannot resolve is indistinguishable from one the
agent invented. Parsing at construction means an unresolvable identifier cannot
enter a diagnosis at all, rather than being detected by a scorer afterwards.

ALIASES ARE CURRENT STATE, NEVER HISTORY
----------------------------------------
`AliasBinding` records what an alias points to NOW and carries no timestamp
range, because Unity Catalog does not expose alias history (19.2a, gap G5).
`ResolvedModel.champion_version_at` deliberately does not exist: there is no
temporal source for it, and a field of that name would be filled in by
somebody eventually.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Outcome vocabulary -----------------------------------------------------


class DegradationStatus(StrEnum):
    """The verdict on the bounded question.

    `INSUFFICIENT_EVIDENCE` is not a failure mode. It is the correct answer
    whenever the evidence cannot support either of the other two, and in the
    current DEV workspace it is the only honest answer available.
    """

    DEGRADED = "degraded"
    NOT_DEGRADED = "not_degraded"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class CauseSupport(StrEnum):
    """How strongly the evidence backs a proposed cause.

    The gap between SUPPORTED and CORRELATION_ONLY is the whole point of the
    product. Two metrics moving together is CORRELATION_ONLY however suggestive
    it looks, and a correlation-only cause is reported as an investigation to
    run, never as a root cause found.
    """

    SUPPORTED = "supported"
    CORRELATION_ONLY = "correlation_only"
    UNSUPPORTED = "unsupported"


class AgentOutcome(StrEnum):
    """What the agent did with the request, independent of the diagnosis."""

    DIAGNOSED = "diagnosed"
    REFUSED = "refused"
    FAILED = "failed"


class RefusalReason(StrEnum):
    """Why a request was refused. Every value is a request the agent will never
    carry out, not a capability that is merely missing today."""

    #: Retrain, promote, move an alias, run a job, write a table.
    STATE_CHANGING_REQUEST = "state_changing_request"
    #: Arbitrary SQL, a workspace path, or an execution instruction.
    UNSAFE_INSTRUCTION = "unsafe_instruction"
    #: The named model is not a governed identifier or is not registered.
    UNKNOWN_MODEL = "unknown_model"
    #: Outside the bounded question entirely.
    OUT_OF_SCOPE = "out_of_scope"
    #: The request exceeded a hard input bound.
    INPUT_REJECTED = "input_rejected"


class ToolRiskLevel(StrEnum):
    """Risk classification.

    STATE_CHANGING exists so the registry can NAME it and refuse to hold it.
    Phase 19.2 registers no state-changing tool at all; declaring the level
    keeps the taxonomy honest and lets `test_policy` assert that the registry is
    empty of them rather than assert against a level that does not exist.
    """

    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"


class SourceType(StrEnum):
    """The kind of authority an evidence reference points at.

    Each value implies an identifier shape, enforced in `EvidenceReference`.
    """

    UNITY_CATALOG_TABLE = "unity_catalog_table"
    #: A SCHEMA's table listing. The authoritative evidence that a table is
    #: ABSENT: citing the missing table itself would be a citation to something
    #: that does not exist, which an operator cannot resolve and which is
    #: indistinguishable from a fabricated one. The schema listing is the thing
    #: that was actually read.
    UNITY_CATALOG_SCHEMA = "unity_catalog_schema"
    UNITY_CATALOG_MODEL = "unity_catalog_model"
    UNITY_CATALOG_MODEL_VERSION = "unity_catalog_model_version"
    MLFLOW_RUN = "mlflow_run"
    JOB_RUN = "job_run"
    REPOSITORY_DOCUMENT = "repository_document"
    REPOSITORY_SOURCE = "repository_source"


# --- Identifier grammars ----------------------------------------------------
#
# Deliberately strict and deliberately small. Each pattern describes an address
# an operator can paste somewhere and resolve. Anything that does not match is
# not "probably fine" — it is a citation nobody can check.

_UC_NAME = r"[A-Za-z0-9_]+"
_TABLE_RE = re.compile(rf"^{_UC_NAME}\.{_UC_NAME}\.{_UC_NAME}$")
_MODEL_RE = re.compile(rf"^{_UC_NAME}\.{_UC_NAME}\.{_UC_NAME}$")
_SCHEMA_RE = re.compile(rf"^{_UC_NAME}\.{_UC_NAME}$")
_MODEL_VERSION_RE = re.compile(rf"^{_UC_NAME}\.{_UC_NAME}\.{_UC_NAME}/versions/[1-9][0-9]*$")
_RUN_RE = re.compile(r"^[0-9a-f]{32}$")
_JOB_RUN_RE = re.compile(r"^jobs/[1-9][0-9]*/runs/[1-9][0-9]*$")
#: A repository-relative path, optionally with a heading anchor. Anchored to
#: forbid absolute paths and parent traversal — an identifier that escaped the
#: repository would name a document nobody reviewed.
_DOC_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]+\.md(?:#[A-Za-z0-9._-]+)?$")
_SOURCE_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]+\.py(?:#[A-Za-z0-9._-]+)?$")

_IDENTIFIER_GRAMMAR: dict[SourceType, re.Pattern[str]] = {
    SourceType.UNITY_CATALOG_TABLE: _TABLE_RE,
    SourceType.UNITY_CATALOG_SCHEMA: _SCHEMA_RE,
    SourceType.UNITY_CATALOG_MODEL: _MODEL_RE,
    SourceType.UNITY_CATALOG_MODEL_VERSION: _MODEL_VERSION_RE,
    SourceType.MLFLOW_RUN: _RUN_RE,
    SourceType.JOB_RUN: _JOB_RUN_RE,
    SourceType.REPOSITORY_DOCUMENT: _DOC_RE,
    SourceType.REPOSITORY_SOURCE: _SOURCE_RE,
}


def identifier_pattern(source_type: SourceType) -> re.Pattern[str]:
    """The grammar one source type's identifier must match.

    Public so an evaluation scorer can re-check validity INDEPENDENTLY rather
    than trusting that the constructor validated. A scorer that only asserts
    "the constructor ran" proves nothing about the constructor.
    """
    return _IDENTIFIER_GRAMMAR[source_type]


#: Hard ceiling on a free-text question. Not a performance guard — a bound on
#: how much attacker-controlled text can reach any downstream consumer.
MAX_QUESTION_CHARS = 2000

#: Hard ceiling on a model identifier.
MAX_MODEL_NAME_CHARS = 200


# --- Time windows -----------------------------------------------------------


class TimeWindow(BaseModel):
    """A closed observation interval, always timezone-aware and always UTC.

    NAIVE DATETIMES ARE REJECTED, NOT ASSUMED TO BE UTC. A naive timestamp in
    monitoring data is genuinely ambiguous, and guessing produces a window that
    is silently wrong by hours — which for a seven-day question is the
    difference between including and excluding a whole day's observations.
    Offset-aware inputs in other zones are converted to UTC so comparisons are
    total.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _must_be_utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("timestamps must be timezone-aware; a naive timestamp is ambiguous")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end < self.start:
            raise ValueError("window end must not precede window start")
        return self

    def covers(self, moment: datetime) -> bool:
        """Inclusive at both ends.

        Inclusive deliberately: an observation written exactly at the window
        boundary is inside the window an operator asked about. Excluding it
        would drop the newest row of a "last seven days" question roughly
        whenever the query ran on a boundary.
        """
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError("timestamps must be timezone-aware; a naive timestamp is ambiguous")
        return self.start <= moment.astimezone(UTC) <= self.end

    @property
    def duration_days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400.0

    def label(self) -> str:
        """A stable, sortable rendering used in evidence envelopes."""
        return f"{self.start.isoformat()}/{self.end.isoformat()}"


# --- Evidence ---------------------------------------------------------------


class EvidenceReference(BaseModel):
    """One resolvable citation.

    `relevant_fields` names the columns or attributes actually read. It is
    required and non-empty: a citation to a whole table tells an operator where
    to start looking, not what was looked at, and the difference matters when
    the claim is "this metric crossed this threshold".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: SourceType
    source_identifier: str = Field(min_length=1, max_length=512)
    observed_at: datetime
    query_window: TimeWindow
    relevant_fields: tuple[str, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def _observed_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _identifier_matches_source_type(self) -> EvidenceReference:
        pattern = _IDENTIFIER_GRAMMAR[self.source_type]
        if not pattern.match(self.source_identifier):
            # Names the rule and the source type, never the offending value:
            # the identifier may be attacker-influenced and this text is logged.
            raise ValueError(
                f"source_identifier does not match the required shape for {self.source_type.value}"
            )
        return self

    @property
    def key(self) -> tuple[str, str]:
        """Identity for de-duplication: the same address cited twice is one
        citation, not two, and counting it twice would inflate evidence
        coverage."""
        return (self.source_type.value, self.source_identifier)


class EvidenceAbsence(BaseModel):
    """A source correctly reporting that the data is not there.

    A FIRST-CLASS RESULT, NOT AN ERROR. `dev.ml_lifecycle_demo.monitoring_history`
    does not exist (19.2a, gaps G1/G2), and that is a fact the diagnosis needs,
    not a fault to swallow. Carrying it as a value means the agent can say
    "the monitoring table does not exist" instead of "something went wrong".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: SourceType
    #: The address that WOULD have held the data. Same grammar as a reference:
    #: an operator must be able to go and confirm the absence.
    source_identifier: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=300)
    query_window: TimeWindow

    @model_validator(mode="after")
    def _identifier_matches_source_type(self) -> EvidenceAbsence:
        if not _IDENTIFIER_GRAMMAR[self.source_type].match(self.source_identifier):
            raise ValueError(
                f"source_identifier does not match the required shape for {self.source_type.value}"
            )
        return self


class MetricThreshold(BaseModel):
    """A version-controlled bound, with the source that defines it.

    The `defined_in` reference is mandatory. A threshold quoted without its
    definition is an assertion, and `permits_degradation_finding` refuses to
    treat an assertion as a bound.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str = Field(min_length=1, max_length=64)
    #: True when breaching means EXCEEDING (rmse, drift, null_rate); False when
    #: breaching means FALLING BELOW (r2).
    breach_above: bool
    value: float
    defined_in: EvidenceReference

    @field_validator("value")
    @classmethod
    def _finite(cls, value: float) -> float:
        if math.isnan(value) or math.isinf(value):
            raise ValueError("a threshold must be a finite number")
        return value

    def is_breached_by(self, observed: float) -> bool:
        """NaN never breaches.

        A NaN metric is an absence of measurement wearing the costume of one.
        Every comparison with NaN is False in IEEE 754, so this would return
        False anyway — it is written explicitly so that the intent survives a
        future refactor that reaches for `not (observed <= self.value)`, which
        would silently turn NaN into a breach.
        """
        if math.isnan(observed):
            return False
        return observed > self.value if self.breach_above else observed < self.value


class MonitoringObservation(BaseModel):
    """One row of `monitoring_history`, typed.

    Mirrors `build_history_row` in
    `products/ml-lifecycle-demo/src/monitoring_logic.py`. Metrics are optional
    because that function writes `drift_score: None` when drift is infinite, and
    a schema that could not represent its own source would force the adapter to
    invent a value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    model_version: int
    status: str
    rmse: float | None = None
    r2: float | None = None
    drift_score: float | None = None
    null_rate: float | None = None
    batch_row_count: int | None = None
    prediction_row_count: int | None = None
    actuals_row_count: int | None = None
    retraining_required: bool = False
    #: Free text from the monitor. NEVER interpreted as instruction — see
    #: policy.scan_untrusted_text.
    reasons: str = ""

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)


class AliasBinding(BaseModel):
    """What an alias points at RIGHT NOW.

    NO TIME RANGE, DELIBERATELY. Unity Catalog exposes current alias state only
    (19.2a, gap G5). A `valid_from` field here would be filled in by inference
    within a week, and "which version was Champion last Tuesday" would start
    being answered from a value nobody observed.

    `name` preserves the original casing for evidence; `matches` compares
    case-insensitively because UC returns `champion` while Phase 15 code writes
    `Champion` (19.2a, gap G9).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)

    def matches(self, alias: str) -> bool:
        return self.name.casefold() == alias.casefold()


class ModelVersionFacts(BaseModel):
    """Registry facts for one version.

    `tags_available` records whether the SOURCE returned tags, separately from
    whether `tags` is empty. Phase 19.2a found that
    `databricks model-versions get` omits tags entirely while the REST endpoint
    returns full provenance (gap: the CLI shape). An adapter reading the CLI
    would see `{}` and an agent that could not tell "no tags" from "tags not
    returned" would report missing provenance as a finding. This flag is what
    keeps that from becoming a fabricated conclusion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    created_at: datetime
    status: str = "READY"
    run_id: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    tags_available: bool = True
    metrics: dict[str, float] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)


class ResolvedModel(BaseModel):
    """A governed model identity.

    There is no `champion_version_at(moment)`. See the module docstring: the
    temporal source does not exist, so the method must not either.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog: str = Field(min_length=1, max_length=64)
    schema_name: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    aliases: tuple[AliasBinding, ...] = ()
    versions: tuple[ModelVersionFacts, ...] = ()

    @property
    def full_name(self) -> str:
        return f"{self.catalog}.{self.schema_name}.{self.name}"

    def alias(self, alias: str) -> AliasBinding | None:
        """Case-insensitive lookup preserving the stored casing."""
        for binding in self.aliases:
            if binding.matches(alias):
                return binding
        return None


class PredictionSample(BaseModel):
    """A scored row.

    `prediction` is a TUPLE because the live column is typed `ARRAY`, not
    `DOUBLE` (19.2a, gap G8). `scalar` is the only sanctioned way to get a
    number out, and it refuses rather than guessing when the array is empty or
    holds more than one element — a multi-output prediction silently reduced to
    its first element is a wrong number presented confidently.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: float
    prediction: tuple[float, ...]
    scored_at: datetime
    model_version: str

    @field_validator("scored_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("scored_at must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def scalar(self) -> float | None:
        if len(self.prediction) != 1:
            return None
        return self.prediction[0]


# --- Diagnosis --------------------------------------------------------------


class LikelyCause(BaseModel):
    """A proposed explanation, with its support level and its citations.

    INVARIANT, ENFORCED HERE: a SUPPORTED cause must cite at least one
    EvidenceReference and must name the runbook mechanism that recognises it.
    A cause claiming support with no citation cannot be constructed, so it
    cannot reach a scorer, a report or an operator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1, max_length=500)
    support: CauseSupport
    evidence: tuple[EvidenceReference, ...] = ()
    #: The runbook section or monitoring rule that recognises this mechanism.
    #: Required for SUPPORTED; the mechanism is what separates a cause from a
    #: coincidence.
    mechanism: EvidenceReference | None = None

    @model_validator(mode="after")
    def _support_requires_evidence_and_mechanism(self) -> LikelyCause:
        if self.support is CauseSupport.SUPPORTED:
            if not self.evidence:
                raise ValueError("a supported cause must cite at least one evidence reference")
            if self.mechanism is None:
                raise ValueError(
                    "a supported cause must name the runbook mechanism that recognises it"
                )
        return self


class Diagnosis(BaseModel):
    """The structured answer. The only thing this agent returns.

    `selected_tools` and `outcome` are part of the answer rather than sidecar
    telemetry because 19.2d scores tool selection and read-only compliance from
    the response itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_model: str
    resolved_model: str | None
    resolved_version: int | None
    time_window: TimeWindow | None
    degradation_status: DegradationStatus
    summary: str = Field(min_length=1, max_length=2000)
    likely_causes: tuple[LikelyCause, ...] = ()
    supporting_evidence: tuple[EvidenceReference, ...] = ()
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    recommended_investigations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    selected_tools: tuple[str, ...] = ()
    outcome: AgentOutcome = AgentOutcome.DIAGNOSED
    refusal_reason: RefusalReason | None = None

    @model_validator(mode="after")
    def _degradation_requires_evidence(self) -> Diagnosis:
        """A `degraded` verdict cannot be constructed without citations.

        The full rule lives in `evidence.permits_degradation_finding`, which
        needs the monitoring series and the threshold to apply it. This is the
        last line: whatever path produced the object, a bare `degraded` with no
        supporting evidence is not representable.
        """
        if self.degradation_status is DegradationStatus.DEGRADED and not self.supporting_evidence:
            raise ValueError("a degraded finding must carry supporting evidence")
        if self.outcome is AgentOutcome.REFUSED and self.refusal_reason is None:
            raise ValueError("a refusal must name its reason")
        # Confidence is bounded by the verdict: claiming high confidence in an
        # absence of evidence is the failure this whole product exists to avoid.
        if (
            self.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE
            and self.confidence > 0.5
        ):
            raise ValueError("insufficient evidence cannot carry confidence above 0.5")
        return self

    def evidence_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(reference.key for reference in self.supporting_evidence)


def deduplicate(references: tuple[EvidenceReference, ...]) -> tuple[EvidenceReference, ...]:
    """Collapse repeated citations, preserving first-seen order.

    Order-preserving so output stays deterministic for identical input, which
    19.2d gates at 100%.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[EvidenceReference] = []
    for reference in references:
        if reference.key in seen:
            continue
        seen.add(reference.key)
        unique.append(reference)
    return tuple(unique)


__all__ = [
    "MAX_MODEL_NAME_CHARS",
    "MAX_QUESTION_CHARS",
    "AgentOutcome",
    "AliasBinding",
    "CauseSupport",
    "DegradationStatus",
    "Diagnosis",
    "EvidenceAbsence",
    "EvidenceReference",
    "LikelyCause",
    "MetricThreshold",
    "ModelVersionFacts",
    "MonitoringObservation",
    "PredictionSample",
    "RefusalReason",
    "ResolvedModel",
    "SourceType",
    "TimeWindow",
    "ToolRiskLevel",
    "deduplicate",
    "identifier_pattern",
]
