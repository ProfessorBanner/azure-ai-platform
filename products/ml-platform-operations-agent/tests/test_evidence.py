"""The evidence rules: coverage, the degradation gate, cause routing."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from ml_platform_operations_agent.config import monitoring_thresholds
from ml_platform_operations_agent.domain import (
    CauseSupport,
    DegradationStatus,
    EvidenceReference,
    LikelyCause,
    MetricThreshold,
    MonitoringObservation,
    SourceType,
    TimeWindow,
)
from ml_platform_operations_agent.evidence import (
    NO_BREACH,
    breaching_observations,
    classify_status,
    confidence_for,
    covers_window,
    demote_unsupported_causes,
    metric_values,
    observations_in_window,
    permits_degradation_finding,
    threshold_direction_is_consistent,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=NOW - timedelta(days=7), end=NOW)
THRESHOLDS = monitoring_thresholds(WINDOW, NOW)


def rows(
    count: int = 7,
    *,
    rmse: float = 10.0,
    r2: float = 0.99,
    drift: float = 0.05,
    null_rate: float = 0.0,
    end: datetime = NOW,
    version: int = 7,
) -> tuple[MonitoringObservation, ...]:
    return tuple(
        MonitoringObservation(
            observed_at=end - timedelta(days=offset),
            model_version=version,
            status="ok",
            rmse=rmse,
            r2=r2,
            drift_score=drift,
            null_rate=null_rate,
        )
        for offset in reversed(range(count))
    )


def reference() -> EvidenceReference:
    return EvidenceReference(
        source_type=SourceType.UNITY_CATALOG_TABLE,
        source_identifier="dev.ml_lifecycle_demo.monitoring_history",
        observed_at=NOW,
        query_window=WINDOW,
        relevant_fields=("rmse",),
    )


# --- windowing --------------------------------------------------------------


def test_observations_are_sorted_regardless_of_input_order() -> None:
    """A SQL result set has no guaranteed order, and determinism is a gate."""
    unsorted = tuple(reversed(rows(5)))
    result = observations_in_window(unsorted, WINDOW)
    assert [r.observed_at for r in result] == sorted(r.observed_at for r in result)


def test_rows_outside_the_window_are_excluded() -> None:
    inside = rows(3)
    outside = rows(2, end=NOW - timedelta(days=30))
    assert len(observations_in_window(inside + outside, WINDOW)) == 3


# --- coverage ---------------------------------------------------------------


def test_full_daily_coverage_is_accepted() -> None:
    assert covers_window(rows(7), WINDOW) is None


def test_no_observations_is_not_coverage() -> None:
    assert covers_window((), WINDOW) is not None


def test_a_single_observation_cannot_establish_a_trend() -> None:
    problem = covers_window(rows(1), WINDOW)
    assert problem is not None
    assert "one monitoring observation" in problem


def test_a_late_start_leaves_the_early_window_unobserved() -> None:
    late = rows(2)
    problem = covers_window(late, WINDOW)
    assert problem is not None
    assert "after the window start" in problem


def test_a_stale_series_is_reported_as_possibly_stopped() -> None:
    stale = rows(3, end=NOW - timedelta(days=4))
    problem = covers_window(stale, WINDOW)
    assert problem is not None
    assert "monitoring may have stopped" in problem


def test_a_large_internal_gap_breaks_coverage() -> None:
    early = rows(2, end=NOW - timedelta(days=6))
    late = rows(2, end=NOW)
    problem = covers_window(early + late, WINDOW)
    assert problem is not None
    assert "gap between consecutive observations" in problem


# --- metric extraction ------------------------------------------------------


def test_nan_and_infinite_values_are_dropped_not_coerced() -> None:
    mixed = rows(1, rmse=math.nan) + rows(1, rmse=math.inf, end=NOW - timedelta(days=1))
    assert metric_values(mixed, "rmse") == ()


def test_none_values_are_dropped() -> None:
    row = MonitoringObservation(observed_at=NOW, model_version=7, status="ok", rmse=None)
    assert metric_values((row,), "rmse") == ()


def test_an_unknown_metric_name_yields_nothing() -> None:
    assert metric_values(rows(3), "not_a_metric") == ()


# --- the degradation gate ---------------------------------------------------


def test_healthy_covered_series_reports_no_breach() -> None:
    assert permits_degradation_finding(rows(7), WINDOW, THRESHOLDS) == NO_BREACH
    status, limitations = classify_status(rows(7), WINDOW, THRESHOLDS)
    assert status is DegradationStatus.NOT_DEGRADED
    assert limitations == ()


def test_a_breach_permits_a_degradation_finding() -> None:
    breaching = rows(7, rmse=25.0, r2=0.5)
    assert permits_degradation_finding(breaching, WINDOW, THRESHOLDS) is None
    status, _ = classify_status(breaching, WINDOW, THRESHOLDS)
    assert status is DegradationStatus.DEGRADED


def test_no_series_abstains_rather_than_reporting_health() -> None:
    status, limitations = classify_status((), WINDOW, THRESHOLDS)
    assert status is DegradationStatus.INSUFFICIENT_EVIDENCE
    assert limitations


def test_no_thresholds_abstains() -> None:
    status, _ = classify_status(rows(7, rmse=25.0), WINDOW, ())
    assert status is DegradationStatus.INSUFFICIENT_EVIDENCE


def test_all_nan_metrics_abstain_rather_than_reporting_health() -> None:
    """AN UNMEASURED MODEL IS NOT A HEALTHY ONE. An operator reading
    `not_degraded` stops looking."""
    unmeasured = rows(7, rmse=math.nan, r2=math.nan, drift=math.nan, null_rate=math.nan)
    status, limitations = classify_status(unmeasured, WINDOW, THRESHOLDS)
    assert status is DegradationStatus.INSUFFICIENT_EVIDENCE
    assert any("no finite value" in item for item in limitations)


def test_one_finite_metric_is_enough_to_judge() -> None:
    partly = rows(7, rmse=math.nan, r2=math.nan, drift=math.nan, null_rate=0.0)
    status, _ = classify_status(partly, WINDOW, THRESHOLDS)
    assert status is DegradationStatus.NOT_DEGRADED


def test_a_breach_outside_the_window_does_not_count() -> None:
    outside = rows(7, rmse=25.0, end=NOW - timedelta(days=30))
    status, _ = classify_status(outside, WINDOW, THRESHOLDS)
    assert status is DegradationStatus.INSUFFICIENT_EVIDENCE


def test_uncovered_window_abstains_even_when_a_metric_breaches() -> None:
    """The dangerous case: a breach exists but the window is not covered, so
    the seven-day question is still unanswered."""
    partial = rows(2, rmse=25.0)
    status, _ = classify_status(partial, WINDOW, THRESHOLDS)
    assert status is DegradationStatus.INSUFFICIENT_EVIDENCE


def test_breaching_observations_are_ordered_and_complete() -> None:
    hits = breaching_observations(rows(7, rmse=25.0, r2=0.5), WINDOW, THRESHOLDS)
    assert hits
    metrics = {threshold.metric for _row, threshold, _value in hits}
    assert metrics == {"rmse", "r2"}
    times = [row.observed_at for row, _t, _v in hits]
    assert times == sorted(times)


# --- threshold direction ----------------------------------------------------


def test_inverted_threshold_direction_is_detected() -> None:
    """`breach_above=True` on r2 would mean degraded when the model explains
    MORE variance, inverting every verdict that depends on it."""
    wrong = MetricThreshold(metric="r2", breach_above=True, value=0.85, defined_in=reference())
    assert not threshold_direction_is_consistent(wrong)


def test_correct_directions_pass() -> None:
    for threshold in THRESHOLDS:
        assert threshold_direction_is_consistent(threshold)


def test_unknown_metrics_are_not_constrained() -> None:
    novel = MetricThreshold(
        metric="latency_ms", breach_above=True, value=100.0, defined_in=reference()
    )
    assert threshold_direction_is_consistent(novel)


# --- cause routing ----------------------------------------------------------


def test_supported_causes_are_asserted_and_others_demoted() -> None:
    supported = LikelyCause(
        statement="drift crossed its threshold",
        support=CauseSupport.SUPPORTED,
        evidence=(reference(),),
        mechanism=reference(),
    )
    correlated = LikelyCause(statement="version changed too", support=CauseSupport.CORRELATION_ONLY)
    unsupported = LikelyCause(statement="cosmic rays", support=CauseSupport.UNSUPPORTED)

    assertable, investigations = demote_unsupported_causes((supported, correlated, unsupported))

    assert assertable == (supported,)
    assert len(investigations) == 2
    assert any("Confirm or rule out" in item for item in investigations)
    assert any("co-movement only" in item for item in investigations)


def test_correlation_only_is_phrased_as_a_question_not_a_finding() -> None:
    correlated = LikelyCause(statement="drift moved too", support=CauseSupport.CORRELATION_ONLY)
    _assertable, investigations = demote_unsupported_causes((correlated,))
    assert investigations[0].startswith("Confirm or rule out")


# --- confidence -------------------------------------------------------------


def test_insufficient_evidence_confidence_stays_under_the_ceiling() -> None:
    assert confidence_for(DegradationStatus.INSUFFICIENT_EVIDENCE, (), ()) <= 0.5
    assert confidence_for(DegradationStatus.INSUFFICIENT_EVIDENCE, (), (reference(),)) <= 0.5


def test_supported_causes_raise_confidence() -> None:
    supported = LikelyCause(
        statement="x",
        support=CauseSupport.SUPPORTED,
        evidence=(reference(),),
        mechanism=reference(),
    )
    with_cause = confidence_for(DegradationStatus.DEGRADED, (supported,), (reference(),))
    without = confidence_for(DegradationStatus.DEGRADED, (), (reference(),))
    assert with_cause > without


def test_confidence_is_capped() -> None:
    supported = LikelyCause(
        statement="x",
        support=CauseSupport.SUPPORTED,
        evidence=(reference(),),
        mechanism=reference(),
    )
    assert (
        confidence_for(DegradationStatus.DEGRADED, (supported,) * 10, (reference(),) * 10) <= 0.95
    )


def test_confidence_is_deterministic() -> None:
    assert confidence_for(DegradationStatus.NOT_DEGRADED, (), ()) == confidence_for(
        DegradationStatus.NOT_DEGRADED, (), ()
    )
