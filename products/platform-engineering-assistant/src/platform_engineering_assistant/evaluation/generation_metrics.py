"""Case outcomes and the precise definitions of every reported metric.

NAMING DISCIPLINE
-----------------
`citation_containment_rate` is NOT called groundedness anywhere in this product,
and the distinction is the reason this module exists as its own file. Containment
proves that every citation names a chunk that was actually retrieved for the
question. It proves nothing about whether that chunk SUPPORTS the sentence
attached to it. Calling it groundedness would let a cheap structural check be
read as a correctness guarantee, which is the most consequential wrong claim this
codebase could make about itself.

Semantic support is estimated separately, by a model, and is labelled
`semantic_groundedness` so that its provenance is visible in its name.

DENOMINATORS ARE STATED, NOT IMPLIED
------------------------------------
Every rate below names the population it is computed over. Rates that share a
numerator but differ in denominator are the standard way an evaluation suite
misleads its own authors, so each definition is written out in full.

REDACTION
---------
A `CaseOutcome` has no field for the question, the answer, the context or a
chunk body. There is no step to remember: the fields do not exist.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from platform_engineering_assistant.domain import AnswerStatus, RefusalReason
from platform_engineering_assistant.errors import FailureCategory
from platform_engineering_assistant.grounding import GroundingViolation

P95 = 0.95


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile. None for an empty sample.

    Nearest-rank rather than an interpolating variant: with sixteen cases,
    interpolation invents a value between two observations and reports a latency
    that never happened. The rank is `ceil(fraction * n)`, so p95 of sixteen
    samples is the 16th — the slowest observed call, which is the honest answer
    at this sample size and is exactly why the sample size is reported alongside.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def mean(values: list[float]) -> float | None:
    """Arithmetic mean, or None for an empty sample."""
    return statistics.fmean(values) if values else None


@dataclass(frozen=True, slots=True)
class JudgeCaseResult:
    """What the judge said about one case. Never carries its rationale."""

    claims_total: int
    claims_unsupported: int
    answer_relevance: float
    refusal_correct: bool | None
    material_contradiction: bool
    groundedness: float
    judge_failed: bool = False


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """One case, described without repeating any of its content."""

    case_id: str
    category: str
    tags: tuple[str, ...]

    expected_disposition: AnswerStatus
    observed_disposition: AnswerStatus | None
    refusal_reason: RefusalReason | None = None
    grounding_violation: GroundingViolation | None = None

    # Set when `service.answer` raised. A failed case produced no evidence about
    # quality, so it is excluded from quality denominators and counted here.
    failure_category: FailureCategory | None = None

    expected_doc_ids: tuple[str, ...] = ()
    cited_chunk_ids: tuple[str, ...] = ()
    cited_doc_ids: tuple[str, ...] = ()
    retrieved_chunk_ids: tuple[str, ...] = ()

    prohibited_hit: bool = False
    answer_chars: int = 0

    # Set by the runner. True when the observed disposition matched the
    # expectation AND, for a refusal, the reason was one the case allows.
    # Carried as a recorded fact rather than recomputed here, because deciding
    # it needs the case's allowed-reason list and this module deliberately never
    # sees the dataset.
    expectation_met: bool = False

    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    judge: JudgeCaseResult | None = field(default=None)

    # --- derived properties, each one a metric's per-case predicate ----------

    @property
    def completed(self) -> bool:
        """A response was produced. Failures are not quality evidence."""
        return self.failure_category is None and self.observed_disposition is not None

    @property
    def schema_failed(self) -> bool:
        """The model's output did not satisfy the grounded-draft schema."""
        return self.failure_category is FailureCategory.INVALID_STRUCTURED_OUTPUT

    @property
    def disposition_correct(self) -> bool:
        """The case behaved as expected.

        Stricter than comparing the two enum values: a refusal for a reason the
        case does not allow is a different behaviour from the one being tested,
        and scoring it as correct would hide a real regression behind a
        correct-looking rate. `observed_disposition` stays truthful throughout,
        so the per-case report shows exactly what happened.
        """
        return self.completed and self.expectation_met

    @property
    def citations_contained(self) -> bool | None:
        """True when every citation names a retrieved chunk. None when not answered.

        `None` rather than `True` for a refusal: a refusal carries no citations,
        so it is not evidence that containment holds, and counting it as a pass
        would let a run of refusals report perfect containment.
        """
        if not self.completed or self.observed_disposition is not AnswerStatus.ANSWERED:
            return None
        retrieved = set(self.retrieved_chunk_ids)
        return all(chunk_id in retrieved for chunk_id in self.cited_chunk_ids)

    @property
    def matched_expected_docs(self) -> int:
        return len(set(self.expected_doc_ids) & set(self.cited_doc_ids))

    @property
    def passed(self) -> bool:
        """The per-case verdict used by the regression comparison.

        Deliberately strict and deliberately deterministic: correct disposition,
        no prohibited content, containment holding where it applies, and no
        provider failure. The judge is not consulted, so a case's pass/fail
        status means the same thing in fake and live runs.
        """
        if not self.completed:
            return False
        if self.prohibited_hit:
            return False
        if self.citations_contained is False:
            return False
        return self.disposition_correct


@dataclass(frozen=True, slots=True)
class GenerationMetrics:
    """Every reported metric. `None` means NOT MEASURABLE, never zero.

    The distinction is load-bearing: a metric that could not be computed must
    reach the gate as an absence, so the gate can report INCOMPLETE rather than
    silently passing a bar nothing was measured against.
    """

    cases: int
    completed_cases: int

    # --- deterministic ------------------------------------------------------
    schema_validity_rate: float | None
    disposition_accuracy: float | None
    refusal_accuracy: float | None
    answerable_case_accuracy: float | None
    citation_containment_rate: float | None
    expected_document_citation_recall: float | None
    prohibited_content_rate: float | None
    provider_failure_rate: float

    latency_mean_ms: float | None
    latency_p95_ms: float | None
    input_tokens_mean: float | None
    input_tokens_p95: float | None
    output_tokens_mean: float | None
    output_tokens_p95: float | None
    total_tokens_mean: float | None
    total_tokens_p95: float | None

    # --- judge (None throughout when the judge did not run) -----------------
    judged_cases: int = 0
    semantic_groundedness: float | None = None
    unsupported_claim_rate: float | None = None
    answer_relevance: float | None = None
    refusal_correctness: float | None = None
    judge_failures: int = 0

    def gate_values(self) -> dict[str, float | None]:
        """The mapping the gate reads. Keys match threshold names exactly."""
        return {
            "schema_validity_rate": self.schema_validity_rate,
            "citation_containment_rate": self.citation_containment_rate,
            "prohibited_content_rate": self.prohibited_content_rate,
            "provider_failure_rate": self.provider_failure_rate,
            "disposition_accuracy": self.disposition_accuracy,
            "refusal_accuracy": self.refusal_accuracy,
            "expected_document_citation_recall": self.expected_document_citation_recall,
            "semantic_groundedness": self.semantic_groundedness,
            "unsupported_claim_rate": self.unsupported_claim_rate,
            "latency_p95_ms": self.latency_p95_ms,
            "total_tokens_p95": self.total_tokens_p95,
        }


def _ratio(numerator: int, denominator: int) -> float | None:
    """A rate, or None when nothing was measured. Never 0/0 as zero."""
    return numerator / denominator if denominator else None


def _token_series(outcomes: list[CaseOutcome], attribute: str) -> list[float]:
    values: list[float] = []
    for outcome in outcomes:
        value = getattr(outcome, attribute)
        if isinstance(value, int):
            values.append(float(value))
    return values


def compute_metrics(outcomes: list[CaseOutcome]) -> GenerationMetrics:
    """Compute every metric from case outcomes. Pure: no I/O, no model.

    METRIC DEFINITIONS, in the order they are computed:

    provider_failure_rate
        cases whose `service.answer` raised a typed error, over ALL cases.

    schema_validity_rate
        cases whose response satisfied the grounded-draft schema, over cases
        that produced a response at all. A case that failed for authentication,
        capacity or timeout produced no output to validate and is EXCLUDED from
        the denominator rather than counted as valid. None when no case produced
        a response.

    disposition_accuracy
        completed cases that met their expectation, over completed cases. For a
        refusal, meeting the expectation includes refusing for an allowed
        reason.

    refusal_accuracy
        completed cases expected to refuse that DID refuse with an allowed
        reason, over completed cases expected to refuse. The allowed-reason
        check is applied by the runner and recorded in `expectation_met`;
        refusing for the wrong reason is not a correct refusal.

    answerable_case_accuracy
        completed cases expected to answer that DID answer, carried at least one
        citation, and hit no prohibited substring — over completed cases expected
        to answer.

    citation_containment_rate
        answered cases whose every citation names a chunk retrieved for that
        question, over answered cases. NOT groundedness.

    expected_document_citation_recall
        micro-averaged: total expected documents cited, over total expected
        documents across all answerable cases. A case that was refused
        contributes zero to the numerator and its full weight to the
        denominator, because failing to answer is failing to cite.

    prohibited_content_rate
        completed cases whose answer text contained any of that case's
        prohibited substrings (case-insensitive), over completed cases.

    latency / token statistics
        over completed cases. Token statistics use only cases where the provider
        actually reported usage; a provider that reports none yields None rather
        than zero.
    """
    total = len(outcomes)
    completed = [outcome for outcome in outcomes if outcome.completed]
    failures = [outcome for outcome in outcomes if outcome.failure_category is not None]

    # A response was produced unless the call failed for a non-schema reason.
    responded = [
        outcome for outcome in outcomes if outcome.failure_category is None or outcome.schema_failed
    ]
    schema_valid = [outcome for outcome in responded if not outcome.schema_failed]

    expected_refusals = [
        outcome for outcome in completed if outcome.expected_disposition is AnswerStatus.REFUSED
    ]
    expected_answers = [
        outcome for outcome in completed if outcome.expected_disposition is AnswerStatus.ANSWERED
    ]
    answered = [
        outcome for outcome in completed if outcome.observed_disposition is AnswerStatus.ANSWERED
    ]

    contained = [outcome for outcome in answered if outcome.citations_contained]

    recall_denominator = sum(
        len(outcome.expected_doc_ids)
        for outcome in outcomes
        if outcome.expected_disposition is AnswerStatus.ANSWERED
    )
    recall_numerator = sum(
        outcome.matched_expected_docs
        for outcome in outcomes
        if outcome.expected_disposition is AnswerStatus.ANSWERED
    )

    answerable_correct = sum(
        1
        for outcome in expected_answers
        if outcome.disposition_correct and outcome.cited_chunk_ids and not outcome.prohibited_hit
    )

    latencies = [outcome.latency_ms for outcome in completed]

    judged = [outcome.judge for outcome in outcomes if outcome.judge is not None]
    scored = [result for result in judged if not result.judge_failed]
    judged_answers = [
        outcome
        for outcome in outcomes
        if outcome.judge is not None
        and not outcome.judge.judge_failed
        and outcome.observed_disposition is AnswerStatus.ANSWERED
    ]
    judged_refusals = [
        result
        for outcome in outcomes
        if outcome.judge is not None
        and not outcome.judge.judge_failed
        and outcome.observed_disposition is AnswerStatus.REFUSED
        for result in (outcome.judge,)
    ]
    claims_total = sum(result.claims_total for result in scored)
    claims_unsupported = sum(result.claims_unsupported for result in scored)

    return GenerationMetrics(
        cases=total,
        completed_cases=len(completed),
        schema_validity_rate=_ratio(len(schema_valid), len(responded)),
        disposition_accuracy=_ratio(
            sum(1 for outcome in completed if outcome.disposition_correct), len(completed)
        ),
        refusal_accuracy=_ratio(
            sum(1 for outcome in expected_refusals if outcome.disposition_correct),
            len(expected_refusals),
        ),
        answerable_case_accuracy=_ratio(answerable_correct, len(expected_answers)),
        citation_containment_rate=_ratio(len(contained), len(answered)),
        expected_document_citation_recall=_ratio(recall_numerator, recall_denominator),
        prohibited_content_rate=_ratio(
            sum(1 for outcome in completed if outcome.prohibited_hit), len(completed)
        ),
        provider_failure_rate=len(failures) / total if total else 0.0,
        latency_mean_ms=mean(latencies),
        latency_p95_ms=percentile(latencies, P95),
        input_tokens_mean=mean(_token_series(completed, "input_tokens")),
        input_tokens_p95=percentile(_token_series(completed, "input_tokens"), P95),
        output_tokens_mean=mean(_token_series(completed, "output_tokens")),
        output_tokens_p95=percentile(_token_series(completed, "output_tokens"), P95),
        total_tokens_mean=mean(_token_series(completed, "total_tokens")),
        total_tokens_p95=percentile(_token_series(completed, "total_tokens"), P95),
        judged_cases=len(scored),
        semantic_groundedness=mean(
            [outcome.judge.groundedness for outcome in judged_answers if outcome.judge]
        ),
        unsupported_claim_rate=_ratio(claims_unsupported, claims_total),
        answer_relevance=mean([result.answer_relevance for result in scored]),
        refusal_correctness=_ratio(
            sum(1 for result in judged_refusals if result.refusal_correct is True),
            len(judged_refusals),
        ),
        judge_failures=sum(1 for result in judged if result.judge_failed),
    )


__all__ = [
    "CaseOutcome",
    "GenerationMetrics",
    "JudgeCaseResult",
    "compute_metrics",
    "mean",
    "percentile",
]
