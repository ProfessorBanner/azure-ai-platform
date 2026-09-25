"""Runner orchestration: no retries, correct denominators, evidence scoring."""

from __future__ import annotations

import pytest

from foundry_capability_lab.domain import RiskClassification
from foundry_capability_lab.errors import (
    AuthorizationError,
    ConfigurationError,
    FailureCategory,
    InvalidStructuredOutputError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from foundry_capability_lab.evaluation.runner import run_evaluation
from tests.evaluation_fakes import (
    AlwaysFailingProvider,
    ConstantProvider,
    ScriptedProvider,
    make_case,
    make_result,
)


def test_every_case_is_attempted_once_per_repetition() -> None:
    cases = [make_case(f"C-{index}") for index in range(4)]
    provider = ConstantProvider()

    run = run_evaluation(provider, cases, repetitions=3)

    assert provider.calls == 12
    assert len(run.outcomes) == 12
    assert run.repetitions == 3
    assert {outcome.repetition for outcome in run.outcomes} == {1, 2, 3}


def test_failures_are_recorded_and_never_retried() -> None:
    """The call count is the proof: 2 cases x 1 repetition is exactly 2 calls."""
    cases = [make_case("A"), make_case("B")]
    provider = ScriptedProvider([ProviderError("boom"), make_result()])

    run = run_evaluation(provider, cases, repetitions=1)

    assert provider.calls == 2
    assert len(run.outcomes) == 2
    failed = [outcome for outcome in run.outcomes if not outcome.schema_valid]
    assert len(failed) == 1
    assert failed[0].failure_category is FailureCategory.PROVIDER_ERROR


def test_a_failed_attempt_still_contributes_to_the_denominator() -> None:
    """A non-aborting failure is recorded and counted, not skipped.

    Uses ProviderError rather than a rate limit: a 429 aborts the run by design,
    which is covered separately.
    """
    cases = [make_case("A")]
    provider = ScriptedProvider([ProviderError("500"), make_result()])

    run = run_evaluation(provider, cases, repetitions=2)

    assert len(run.outcomes) == 2
    assert sum(1 for o in run.outcomes if o.schema_valid) == 1


def test_failed_attempts_record_a_latency() -> None:
    run = run_evaluation(
        AlwaysFailingProvider(ProviderError("boom")), [make_case("A")], repetitions=1
    )
    assert run.outcomes[0].latency_ms >= 0.0


def test_ground_truth_is_carried_onto_each_outcome() -> None:
    case = make_case("A", classification=RiskClassification.COST, escalate=False)
    run = run_evaluation(ConstantProvider(), [case], repetitions=1)
    outcome = run.outcomes[0]
    assert outcome.expected_classification is RiskClassification.COST
    assert outcome.expected_requires_escalation is False


# --- evidence scoring -------------------------------------------------------


def test_evidence_within_the_allowed_set_is_valid() -> None:
    case = make_case("A", evidence=["EV-100", "EV-200"])
    provider = ConstantProvider(make_result(evidence=["EV-100"]))
    outcome = run_evaluation(provider, [case], repetitions=1).outcomes[0]
    assert outcome.evidence_valid is True
    assert outcome.fabricated_evidence_count == 0


def test_fabricated_evidence_is_detected_and_counted() -> None:
    case = make_case("A", evidence=["EV-100"])
    provider = ConstantProvider(make_result(evidence=["EV-100", "EV-999", "EV-888"]))
    outcome = run_evaluation(provider, [case], repetitions=1).outcomes[0]
    assert outcome.evidence_valid is False
    assert outcome.fabricated_evidence_count == 2


def test_citing_no_evidence_is_valid_not_fabrication() -> None:
    case = make_case("A", evidence=["EV-100"])
    provider = ConstantProvider(make_result(evidence=[]))
    outcome = run_evaluation(provider, [case], repetitions=1).outcomes[0]
    assert outcome.evidence_valid is True


# --- operational-failure detection ------------------------------------------


def test_access_failure_aborts_and_marks_the_run_incomplete() -> None:
    provider = AlwaysFailingProvider(AuthorizationError("403"))
    run = run_evaluation(provider, [make_case("A"), make_case("B")], repetitions=2)

    assert run.aborted_on is FailureCategory.AUTHORIZATION
    assert run.operationally_complete is False
    # Stopped on the first failure rather than grinding through all four.
    assert provider.calls == 1
    assert len(run.outcomes) == 1
    assert run.planned_attempts == 4


def test_provider_errors_are_signal_and_do_not_abort() -> None:
    """A 500 is measurable reliability evidence; a 403 means we were never let in."""
    provider = AlwaysFailingProvider(ProviderError("500"))
    run = run_evaluation(provider, [make_case("A")], repetitions=2)

    assert run.aborted_on is None
    assert run.operationally_complete is True
    assert provider.calls == 2


def test_zero_repetitions_is_rejected() -> None:
    with pytest.raises(ValueError):
        run_evaluation(ConstantProvider(), [make_case("A")], repetitions=0)


def test_outcomes_carry_no_rationale_or_observation_text() -> None:
    """Redaction is structural: the fields do not exist on the outcome."""
    run = run_evaluation(ConstantProvider(), [make_case("A")], repetitions=1)
    serialised = run.outcomes[0].model_dump()
    assert "rationale" not in serialised
    assert "observation" not in serialised


# --- pacing -----------------------------------------------------------------


class RecordingSleeper:
    """Records requested delays without spending them."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def test_n_attempts_produce_exactly_n_minus_one_sleeps() -> None:
    """Never before the first attempt, never after the last."""
    sleeper = RecordingSleeper()
    cases = [make_case(f"C-{index}") for index in range(4)]

    run = run_evaluation(
        ConstantProvider(),
        cases,
        repetitions=3,
        inter_attempt_delay_seconds=2.5,
        sleeper=sleeper,
    )

    assert len(run.outcomes) == 12
    assert len(sleeper.waits) == 11
    assert run.sleeps == 11
    assert set(sleeper.waits) == {2.5}


def test_single_attempt_never_sleeps() -> None:
    sleeper = RecordingSleeper()
    run_evaluation(
        ConstantProvider(),
        [make_case("A")],
        repetitions=1,
        inter_attempt_delay_seconds=5.0,
        sleeper=sleeper,
    )
    assert sleeper.waits == []


def test_zero_delay_performs_no_sleeps() -> None:
    sleeper = RecordingSleeper()
    run_evaluation(
        ConstantProvider(),
        [make_case("A"), make_case("B")],
        repetitions=2,
        inter_attempt_delay_seconds=0.0,
        sleeper=sleeper,
    )
    assert sleeper.waits == []


def test_pacing_does_not_change_attempt_counts_or_denominators() -> None:
    """The same run, paced and unpaced, must produce identical outcomes."""
    cases = [make_case(f"C-{index}") for index in range(3)]

    unpaced = run_evaluation(ConstantProvider(), cases, repetitions=2)
    paced = run_evaluation(
        ConstantProvider(),
        cases,
        repetitions=2,
        inter_attempt_delay_seconds=9.0,
        sleeper=RecordingSleeper(),
    )

    assert len(paced.outcomes) == len(unpaced.outcomes)
    assert paced.planned_attempts == unpaced.planned_attempts
    assert [o.case_id for o in paced.outcomes] == [o.case_id for o in unpaced.outcomes]


def test_negative_delay_is_rejected() -> None:
    with pytest.raises(ValueError):
        run_evaluation(
            ConstantProvider(), [make_case("A")], repetitions=1, inter_attempt_delay_seconds=-1.0
        )


def test_pacing_delay_is_not_counted_as_latency() -> None:
    """The wait happens outside the timed region, so it must not inflate p95."""
    slow_sleeper = RecordingSleeper()
    run = run_evaluation(
        ConstantProvider(),
        [make_case("A"), make_case("B")],
        repetitions=1,
        inter_attempt_delay_seconds=30.0,
        sleeper=slow_sleeper,
    )
    assert all(outcome.latency_ms < 1000.0 for outcome in run.outcomes)


# --- abort semantics --------------------------------------------------------


def test_rate_limiting_aborts_the_remaining_calls() -> None:
    """Continuing would extend the throttle and collect identical errors."""
    provider = AlwaysFailingProvider(RateLimitedError("429"))
    cases = [make_case(f"C-{index}") for index in range(12)]

    run = run_evaluation(provider, cases, repetitions=3)

    assert provider.calls == 1
    assert run.aborted_on is FailureCategory.RATE_LIMITED
    assert run.operationally_complete is False
    assert run.planned_attempts == 36


def test_structural_404_configuration_error_aborts() -> None:
    provider = AlwaysFailingProvider(ConfigurationError("endpoint not found (404)"))
    run = run_evaluation(provider, [make_case("A")], repetitions=3)

    assert run.aborted_on is FailureCategory.CONFIGURATION
    assert provider.calls == 1


def test_schema_invalid_output_is_quality_evidence_and_does_not_abort() -> None:
    provider = AlwaysFailingProvider(InvalidStructuredOutputError("bad shape"))
    run = run_evaluation(provider, [make_case("A")], repetitions=2)

    assert run.aborted_on is None
    assert run.operationally_complete is True
    assert provider.calls == 2


def test_timeouts_do_not_abort() -> None:
    provider = AlwaysFailingProvider(TimeoutError_("slow"))
    run = run_evaluation(provider, [make_case("A")], repetitions=2)
    assert run.aborted_on is None
    assert provider.calls == 2


def test_abort_preserves_the_outcomes_collected_so_far() -> None:
    """The partial record is the diagnostic, so it must survive the abort."""
    provider = ScriptedProvider([make_result(), RateLimitedError("429"), make_result()])
    run = run_evaluation(provider, [make_case("A"), make_case("B"), make_case("C")], repetitions=1)

    assert len(run.outcomes) == 2
    assert run.outcomes[0].schema_valid is True
    assert run.outcomes[1].failure_category is FailureCategory.RATE_LIMITED
    assert run.operationally_complete is False


def test_a_complete_run_is_operationally_complete() -> None:
    run = run_evaluation(ConstantProvider(), [make_case("A")], repetitions=3)
    assert run.operationally_complete is True
    assert run.aborted_on is None
    assert len(run.outcomes) == run.planned_attempts
