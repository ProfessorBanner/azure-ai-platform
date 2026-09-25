"""Metric definitions: denominators, absences, and the containment/groundedness line."""

from __future__ import annotations

import pytest

from platform_engineering_assistant.domain import AnswerStatus
from platform_engineering_assistant.errors import FailureCategory
from platform_engineering_assistant.evaluation.generation_metrics import (
    JudgeCaseResult,
    compute_metrics,
    mean,
    percentile,
)
from tests.evaluation_fakes import outcome

REFUSED = AnswerStatus.REFUSED
ANSWERED = AnswerStatus.ANSWERED


# --- statistics -------------------------------------------------------------


def test_percentile_of_an_empty_sample_is_none() -> None:
    """Not zero. Zero latency is a claim; no measurement is not."""
    assert percentile([], 0.95) is None
    assert mean([]) is None


def test_p95_is_nearest_rank_and_never_interpolates() -> None:
    """With sixteen samples the p95 is the slowest observed call, not an average.

    An interpolating percentile would report a latency that never happened.
    """
    values = [float(n) for n in range(1, 17)]
    assert percentile(values, 0.95) == 16.0


def test_p95_of_a_single_sample_is_that_sample() -> None:
    assert percentile([42.0], 0.95) == 42.0


# --- rates and their denominators -------------------------------------------


def test_a_rate_over_an_empty_population_is_none_not_zero() -> None:
    """0/0 must reach the gate as an absence so it can report INCOMPLETE."""
    metrics = compute_metrics([outcome(expected=ANSWERED, observed=ANSWERED)])
    assert metrics.refusal_accuracy is None


def test_provider_failures_are_excluded_from_quality_denominators() -> None:
    """A failed call produced no evidence about quality."""
    outcomes = [
        outcome("ok"),
        outcome("boom", observed=None, failure=FailureCategory.TIMEOUT),
    ]
    metrics = compute_metrics(outcomes)
    assert metrics.completed_cases == 1
    assert metrics.provider_failure_rate == 0.5
    assert metrics.disposition_accuracy == 1.0


def test_a_non_schema_failure_is_not_counted_as_schema_valid() -> None:
    """A call that never returned has no output to validate; it leaves the denominator."""
    outcomes = [
        outcome("ok"),
        outcome("auth", observed=None, failure=FailureCategory.AUTHENTICATION),
    ]
    assert compute_metrics(outcomes).schema_validity_rate == 1.0


def test_a_schema_failure_lowers_the_schema_rate() -> None:
    outcomes = [
        outcome("ok"),
        outcome("bad", observed=None, failure=FailureCategory.INVALID_STRUCTURED_OUTPUT),
    ]
    assert compute_metrics(outcomes).schema_validity_rate == 0.5


def test_refusal_accuracy_counts_only_expected_refusals() -> None:
    outcomes = [
        outcome("a", expected=ANSWERED, observed=ANSWERED, expectation_met=True),
        outcome(
            "b",
            expected=REFUSED,
            observed=REFUSED,
            expectation_met=True,
            cited=(),
            cited_docs=(),
            expected_docs=(),
        ),
        outcome("c", expected=REFUSED, observed=ANSWERED, expectation_met=False, expected_docs=()),
    ]
    metrics = compute_metrics(outcomes)
    assert metrics.refusal_accuracy == 0.5
    assert metrics.disposition_accuracy == pytest.approx(2 / 3)


def test_a_refusal_for_a_disallowed_reason_is_not_a_correct_refusal() -> None:
    """`expectation_met` carries the allowed-reason check made by the runner."""
    outcomes = [
        outcome(
            "wrong-reason",
            expected=REFUSED,
            observed=REFUSED,
            expectation_met=False,
            cited=(),
            cited_docs=(),
            expected_docs=(),
        )
    ]
    assert compute_metrics(outcomes).refusal_accuracy == 0.0


# --- containment is not groundedness ----------------------------------------


def test_containment_is_none_for_a_refusal_not_true() -> None:
    """A refusal carries no citations and is therefore no evidence of containment.

    Counting it as a pass would let a run of refusals report perfect containment.
    """
    refusal = outcome(
        "r", expected=REFUSED, observed=REFUSED, cited=(), cited_docs=(), expected_docs=()
    )
    assert refusal.citations_contained is None
    assert compute_metrics([refusal]).citation_containment_rate is None


def test_a_citation_outside_the_retrieved_set_breaks_containment() -> None:
    breached = outcome("x", cited=("fabricated::chunk::0::0",), retrieved=("real::chunk::0::0",))
    assert breached.citations_contained is False
    assert compute_metrics([breached]).citation_containment_rate == 0.0


def test_containment_holds_when_every_citation_was_retrieved() -> None:
    ok = outcome("x", cited=("a::b::0::0",), retrieved=("a::b::0::0", "c::d::0::0"))
    assert ok.citations_contained is True
    assert compute_metrics([ok]).citation_containment_rate == 1.0


# --- expected-document recall ------------------------------------------------


def test_recall_is_micro_averaged_over_expected_documents() -> None:
    outcomes = [
        outcome("one", expected_docs=("adr-0001", "adr-0003"), cited_docs=("adr-0003",)),
        outcome("two", expected_docs=("adr-0002",), cited_docs=("adr-0002",)),
    ]
    assert compute_metrics(outcomes).expected_document_citation_recall == pytest.approx(2 / 3)


def test_a_refused_answerable_case_contributes_its_full_weight_to_the_denominator() -> None:
    """Failing to answer is failing to cite; it must not vanish from the metric."""
    outcomes = [
        outcome(
            "refused-but-answerable",
            expected=ANSWERED,
            observed=REFUSED,
            expectation_met=False,
            cited=(),
            cited_docs=(),
            expected_docs=("adr-0001",),
        )
    ]
    assert compute_metrics(outcomes).expected_document_citation_recall == 0.0


# --- answerable-case accuracy ------------------------------------------------


def test_an_answer_with_prohibited_content_is_not_a_correct_answerable_case() -> None:
    outcomes = [outcome("x", prohibited_hit=True)]
    metrics = compute_metrics(outcomes)
    assert metrics.answerable_case_accuracy == 0.0
    assert metrics.prohibited_content_rate == 1.0


def test_an_answer_without_citations_is_not_a_correct_answerable_case() -> None:
    outcomes = [outcome("x", cited=(), cited_docs=())]
    assert compute_metrics(outcomes).answerable_case_accuracy == 0.0


# --- judge metrics ----------------------------------------------------------


def _judge(
    total: int,
    unsupported: int,
    *,
    relevance: float = 1.0,
    refusal: bool | None = None,
    contradiction: bool = False,
    failed: bool = False,
) -> JudgeCaseResult:
    groundedness = 0.0 if contradiction else (1.0 if total == 0 else (total - unsupported) / total)
    return JudgeCaseResult(
        claims_total=total,
        claims_unsupported=unsupported,
        answer_relevance=relevance,
        refusal_correct=refusal,
        material_contradiction=contradiction,
        groundedness=groundedness,
        judge_failed=failed,
    )


def test_judge_metrics_are_none_when_the_judge_did_not_run() -> None:
    metrics = compute_metrics([outcome("x")])
    assert metrics.semantic_groundedness is None
    assert metrics.unsupported_claim_rate is None
    assert metrics.judged_cases == 0


def test_unsupported_claim_rate_is_computed_over_claims_not_cases() -> None:
    """One long ungrounded answer must not be diluted by many short correct ones."""
    outcomes = [
        outcome("long", judge=_judge(10, 5)),
        outcome("short", judge=_judge(1, 0)),
    ]
    assert compute_metrics(outcomes).unsupported_claim_rate == pytest.approx(5 / 11)


def test_semantic_groundedness_averages_over_answered_cases_only() -> None:
    outcomes = [
        outcome("answered", judge=_judge(4, 1)),
        outcome(
            "refused",
            expected=REFUSED,
            observed=REFUSED,
            cited=(),
            cited_docs=(),
            expected_docs=(),
            judge=_judge(0, 0, refusal=True),
        ),
    ]
    assert compute_metrics(outcomes).semantic_groundedness == pytest.approx(0.75)


def test_refusal_correctness_is_computed_over_refusals() -> None:
    outcomes = [
        outcome(
            "r1",
            expected=REFUSED,
            observed=REFUSED,
            cited=(),
            cited_docs=(),
            expected_docs=(),
            judge=_judge(0, 0, refusal=True),
        ),
        outcome(
            "r2",
            expected=REFUSED,
            observed=REFUSED,
            cited=(),
            cited_docs=(),
            expected_docs=(),
            judge=_judge(0, 0, refusal=False),
        ),
    ]
    assert compute_metrics(outcomes).refusal_correctness == 0.5


def test_a_failed_judgement_is_counted_and_excluded_from_the_scores() -> None:
    outcomes = [
        outcome("ok", judge=_judge(2, 0)),
        outcome("broken", judge=_judge(0, 0, failed=True)),
    ]
    metrics = compute_metrics(outcomes)
    assert metrics.judge_failures == 1
    assert metrics.judged_cases == 1
    assert metrics.semantic_groundedness == 1.0


# --- gate mapping -----------------------------------------------------------


def test_gate_values_expose_every_gated_metric_name() -> None:
    from platform_engineering_assistant.evaluation.policy import load_policy

    values = compute_metrics([outcome("x")]).gate_values()
    assert set(load_policy().thresholds) <= set(values)
