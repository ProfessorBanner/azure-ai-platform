"""Unit tests for the monitoring and retraining-decision contract.

Pure logic only: no Spark, no Databricks, no MLflow, no credential. Spark
computes the aggregates in production; everything asserted here is a judgement
made from those numbers, which is the part worth arguing about.
"""

from __future__ import annotations

import math

import pytest

from ml_logic import (
    BATCH_ROWS,
    FEATURE_COLUMNS,
    ID_COLUMN,
    LABEL_COLUMN,
    TRAINING_ROWS,
    actuals_records,
    batch_input_records,
)
from monitoring_logic import (
    ALERT_DATA_QUALITY,
    ALERT_DRIFT,
    ALERT_FORCED_RETRAIN,
    ALERT_PERFORMANCE,
    MAX_FEATURE_DRIFT,
    MAX_NULL_RATE,
    MONITOR_MAX_RMSE,
    MONITOR_MIN_R2,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_CRITICAL,
    STATUS_OK,
    STATUS_RETRAIN,
    DataQualityResult,
    DriftResult,
    FeatureStats,
    PerformanceResult,
    TableStats,
    build_alert_rows,
    build_history_row,
    check_data_quality,
    compute_drift,
    decide,
    evaluate_performance,
    standardized_mean_difference,
)

OBSERVED_AT = "2026-08-27T06:00:00+00:00"
SHA = "1111111111111111111111111111111111111111"


def batch_stats(rows: int = 100, nulls: int = 0) -> TableStats:
    return TableStats(
        name="prod.ml_lifecycle_demo.batch_input",
        row_count=rows,
        columns=(ID_COLUMN, *FEATURE_COLUMNS),
        null_counts={FEATURE_COLUMNS[0]: nulls},
    )


def prediction_stats(rows: int = 100, nulls: int = 0) -> TableStats:
    return TableStats(
        name="prod.ml_lifecycle_demo.predictions",
        row_count=rows,
        columns=(ID_COLUMN, "prediction"),
        null_counts={"prediction": nulls},
    )


def actuals_stats(rows: int = 100, nulls: int = 0) -> TableStats:
    return TableStats(
        name="prod.ml_lifecycle_demo.inference_actuals",
        row_count=rows,
        columns=(ID_COLUMN, LABEL_COLUMN),
        null_counts={LABEL_COLUMN: nulls},
    )


def healthy_quality() -> DataQualityResult:
    return check_data_quality(batch_stats(), prediction_stats(), actuals_stats())


def no_drift() -> DriftResult:
    return DriftResult(
        per_feature=dict.fromkeys(FEATURE_COLUMNS, 0.05), max_drift=0.05, drifted_features=()
    )


def bad_drift() -> DriftResult:
    return DriftResult(
        per_feature={**dict.fromkeys(FEATURE_COLUMNS, 0.05), FEATURE_COLUMNS[1]: 0.9},
        max_drift=0.9,
        drifted_features=(FEATURE_COLUMNS[1],),
    )


def good_performance() -> PerformanceResult:
    return evaluate_performance(rmse=10.0, r2=0.95)


def bad_performance() -> PerformanceResult:
    return evaluate_performance(rmse=MONITOR_MAX_RMSE + 5.0, r2=MONITOR_MIN_R2 - 0.2)


# --- Delayed ground truth ---------------------------------------------------


def test_actuals_cover_exactly_the_batch_rows() -> None:
    actuals = actuals_records()
    batch = batch_input_records()

    assert len(actuals) == BATCH_ROWS
    assert [row[ID_COLUMN] for row in actuals] == [row[ID_COLUMN] for row in batch]
    assert actuals[0][ID_COLUMN] == float(TRAINING_ROWS)


def test_actuals_carry_no_features() -> None:
    """The structural guarantee that scoring cannot read its own answers."""
    for row in actuals_records()[:5]:
        assert set(row) == {ID_COLUMN, LABEL_COLUMN}
        assert not any(name in row for name in FEATURE_COLUMNS)


def test_actuals_are_deterministic() -> None:
    assert actuals_records() == actuals_records()


# --- Data quality -----------------------------------------------------------


def test_healthy_tables_pass() -> None:
    assert healthy_quality().passed


def test_missing_required_column_fails() -> None:
    broken = TableStats(name="predictions", row_count=100, columns=(ID_COLUMN,))

    result = check_data_quality(batch_stats(), broken, actuals_stats())

    assert not result.passed
    assert any("missing required columns: prediction" in f for f in result.failures)


@pytest.mark.parametrize("table", ["batch", "predictions", "actuals"])
def test_an_empty_table_fails(table: str) -> None:
    result = check_data_quality(
        batch_stats(rows=0 if table == "batch" else 100),
        prediction_stats(rows=0 if table == "predictions" else 100),
        actuals_stats(rows=0 if table == "actuals" else 100),
    )

    assert not result.passed
    assert any("is empty" in f for f in result.failures)


def test_prediction_count_must_equal_batch_count() -> None:
    """Rows silently dropped between scoring and writing would show up nowhere else."""
    result = check_data_quality(batch_stats(rows=100), prediction_stats(rows=97), actuals_stats())

    assert not result.passed
    assert any("does not equal batch input count 100" in f for f in result.failures)


def test_null_rate_above_the_threshold_fails() -> None:
    # 40 nulls over 100 rows x 2 columns = 0.20, well above MAX_NULL_RATE.
    result = check_data_quality(batch_stats(), prediction_stats(nulls=40), actuals_stats())

    assert not result.passed
    assert any("null rate" in f for f in result.failures)
    assert result.null_rate > MAX_NULL_RATE


def test_a_null_rate_at_the_threshold_passes() -> None:
    """The check is 'exceeds', so the boundary itself is acceptable."""
    stats = TableStats(
        name="predictions",
        row_count=100,
        columns=(ID_COLUMN, "prediction"),
        null_counts={"prediction": 2},  # 2 / 200 cells = 0.01 == MAX_NULL_RATE
    )

    assert stats.null_rate == pytest.approx(MAX_NULL_RATE)
    assert check_data_quality(batch_stats(), stats, actuals_stats()).passed


def test_null_rate_of_an_empty_table_is_zero_not_a_division_error() -> None:
    assert TableStats(name="t", row_count=0, columns=("a",)).null_rate == 0.0


# --- Drift ------------------------------------------------------------------


def test_identical_distributions_have_zero_drift() -> None:
    stats = FeatureStats(mean=1.0, stddev=2.0)

    assert standardized_mean_difference(stats, stats) == 0.0


def test_smd_is_the_mean_shift_in_training_standard_deviations() -> None:
    training = FeatureStats(mean=0.0, stddev=2.0)
    batch = FeatureStats(mean=1.0, stddev=2.0)

    assert standardized_mean_difference(training, batch) == pytest.approx(0.5)


def test_smd_is_symmetric_in_direction() -> None:
    """A drop and a rise of equal size are equally drifted."""
    training = FeatureStats(mean=0.0, stddev=1.0)

    assert standardized_mean_difference(
        training, FeatureStats(mean=-0.3, stddev=1.0)
    ) == pytest.approx(standardized_mean_difference(training, FeatureStats(mean=0.3, stddev=1.0)))


def test_a_constant_training_feature_that_moved_is_infinite_drift() -> None:
    """No scale to standardise against, so any movement is unbounded — not a crash."""
    score = standardized_mean_difference(
        FeatureStats(mean=1.0, stddev=0.0), FeatureStats(mean=2.0, stddev=0.0)
    )

    assert math.isinf(score)


def test_a_constant_training_feature_that_did_not_move_is_zero_drift() -> None:
    stats = FeatureStats(mean=1.0, stddev=0.0)

    assert standardized_mean_difference(stats, stats) == 0.0


def test_drift_reports_the_maximum_not_the_mean() -> None:
    """One badly drifted feature must not be averaged away by three stable ones."""
    training = {name: FeatureStats(mean=0.0, stddev=1.0) for name in FEATURE_COLUMNS}
    batch = dict.fromkeys(FEATURE_COLUMNS, FeatureStats(mean=0.0, stddev=1.0))
    batch[FEATURE_COLUMNS[2]] = FeatureStats(mean=0.8, stddev=1.0)

    result = compute_drift(training, batch)

    assert result.max_drift == pytest.approx(0.8)
    assert result.drifted_features == (FEATURE_COLUMNS[2],)
    assert not result.passed


def test_drift_at_the_threshold_passes() -> None:
    training = {name: FeatureStats(mean=0.0, stddev=1.0) for name in FEATURE_COLUMNS}
    batch = dict.fromkeys(FEATURE_COLUMNS, FeatureStats(mean=MAX_FEATURE_DRIFT, stddev=1.0))

    result = compute_drift(training, batch)

    assert result.max_drift == pytest.approx(MAX_FEATURE_DRIFT)
    assert result.passed


def test_drift_fails_closed_on_missing_feature_statistics() -> None:
    training = {name: FeatureStats(mean=0.0, stddev=1.0) for name in FEATURE_COLUMNS}

    with pytest.raises(KeyError, match="missing feature statistics"):
        compute_drift(training, {FEATURE_COLUMNS[0]: FeatureStats(mean=0.0, stddev=1.0)})


# --- Performance ------------------------------------------------------------


def test_good_live_metrics_pass() -> None:
    assert good_performance().passed


def test_excessive_live_rmse_fails() -> None:
    result = evaluate_performance(rmse=MONITOR_MAX_RMSE + 0.01, r2=0.99)

    assert not result.passed
    assert any("RMSE" in f for f in result.failures)


def test_insufficient_live_r2_fails() -> None:
    result = evaluate_performance(rmse=1.0, r2=MONITOR_MIN_R2 - 0.01)

    assert not result.passed
    assert any("R2" in f for f in result.failures)


def test_live_metrics_at_the_boundary_pass() -> None:
    assert evaluate_performance(rmse=MONITOR_MAX_RMSE, r2=MONITOR_MIN_R2).passed


def test_monitor_bounds_are_looser_than_the_training_gate() -> None:
    """A deployed model on moved-on data is judged less harshly than a fresh fit.

    If these ever crossed, every monitor run would demand a retrain that the
    training gate would then reject — an infinite, expensive loop.
    """
    from ml_logic import MAX_RMSE, MIN_R2

    assert MONITOR_MAX_RMSE > MAX_RMSE
    assert MONITOR_MIN_R2 < MIN_R2


# --- The retraining decision ------------------------------------------------


def test_a_healthy_system_requests_nothing() -> None:
    decision = decide(healthy_quality(), no_drift(), good_performance())

    assert decision.status == STATUS_OK
    assert not decision.retraining_required
    assert not decision.forced


def test_drift_requests_retraining() -> None:
    decision = decide(healthy_quality(), bad_drift(), good_performance())

    assert decision.status == STATUS_RETRAIN
    assert decision.retraining_required
    assert any("drift" in r for r in decision.reasons)


def test_poor_performance_requests_retraining() -> None:
    decision = decide(healthy_quality(), no_drift(), bad_performance())

    assert decision.status == STATUS_RETRAIN
    assert decision.retraining_required


def test_critical_data_quality_fails_and_does_not_request_retraining() -> None:
    """Retraining on broken data would fit a model to the breakage."""
    quality = check_data_quality(batch_stats(rows=0), prediction_stats(), actuals_stats())

    decision = decide(quality, bad_drift(), bad_performance())

    assert decision.status == STATUS_CRITICAL
    assert decision.is_critical
    assert not decision.retraining_required


def test_data_quality_outranks_drift_and_performance() -> None:
    """Even with everything else screaming, a broken pipeline is the finding."""
    quality = check_data_quality(batch_stats(rows=100), prediction_stats(rows=1), actuals_stats())

    decision = decide(quality, bad_drift(), bad_performance())

    assert decision.status == STATUS_CRITICAL
    assert all("drift" not in r for r in decision.reasons)


# --- Forced retraining ------------------------------------------------------


def test_force_retrain_requests_retraining_on_a_healthy_system() -> None:
    decision = decide(healthy_quality(), no_drift(), good_performance(), force_retrain=True)

    assert decision.status == STATUS_RETRAIN
    assert decision.retraining_required
    assert decision.forced
    assert any("manually forced" in r for r in decision.reasons)


def test_force_retrain_is_recorded_as_forced_not_as_drift() -> None:
    """The audit record must not misattribute an operator action to the data."""
    decision = decide(healthy_quality(), no_drift(), good_performance(), force_retrain=True)

    assert decision.forced
    assert all("drift" not in r for r in decision.reasons)


def test_force_retrain_does_not_override_critical_data_quality() -> None:
    """Forcing changes WHETHER retraining is requested, not whether data is sane."""
    quality = check_data_quality(batch_stats(rows=0), prediction_stats(), actuals_stats())

    decision = decide(quality, no_drift(), good_performance(), force_retrain=True)

    assert decision.status == STATUS_CRITICAL
    assert not decision.retraining_required


def test_not_forcing_leaves_forced_false() -> None:
    assert not decide(healthy_quality(), bad_drift(), good_performance()).forced


# --- Durable records --------------------------------------------------------


def history_row(**overrides: object) -> dict[str, object]:
    defaults = {
        "observed_at": OBSERVED_AT,
        "environment": "prod",
        "model_name": "prod.ml_lifecycle_demo.linear_regression_model",
        "model_version": 7,
        "quality": healthy_quality(),
        "drift": no_drift(),
        "performance": good_performance(),
        "decision": decide(healthy_quality(), no_drift(), good_performance()),
        "git_sha": SHA,
    }
    defaults.update(overrides)
    return build_history_row(**defaults)  # type: ignore[arg-type]


def test_history_row_records_every_required_field() -> None:
    row = history_row()

    assert set(row) == {
        "observed_at",
        "environment",
        "model_name",
        "model_version",
        "batch_row_count",
        "prediction_row_count",
        "actuals_row_count",
        "null_rate",
        "drift_score",
        "rmse",
        "r2",
        "status",
        "retraining_required",
        "retraining_forced",
        "reasons",
        "git_sha",
    }


def test_history_row_is_written_even_when_everything_is_healthy() -> None:
    """A table that only records problems cannot answer 'when did this start?'."""
    row = history_row()

    assert row["status"] == STATUS_OK
    assert row["retraining_required"] is False


def test_history_row_carries_the_champion_identity_and_provenance() -> None:
    row = history_row()

    assert row["model_name"] == "prod.ml_lifecycle_demo.linear_regression_model"
    assert row["model_version"] == 7
    assert row["environment"] == "prod"
    assert row["git_sha"] == SHA


def test_history_row_tolerates_an_absent_git_sha() -> None:
    assert history_row(git_sha=None)["git_sha"] == ""


def test_history_row_stores_infinite_drift_as_null() -> None:
    """Delta has no infinity for a double column; null is the honest encoding."""
    infinite = DriftResult(
        per_feature={FEATURE_COLUMNS[0]: math.inf},
        max_drift=math.inf,
        drifted_features=(FEATURE_COLUMNS[0],),
    )

    assert history_row(drift=infinite)["drift_score"] is None


def test_history_row_records_a_forced_retrain_distinctly() -> None:
    forced = decide(healthy_quality(), no_drift(), good_performance(), force_retrain=True)

    row = history_row(decision=forced)

    assert row["retraining_required"] is True
    assert row["retraining_forced"] is True


def alerts(**overrides: object) -> list[dict[str, object]]:
    defaults = {
        "observed_at": OBSERVED_AT,
        "quality": healthy_quality(),
        "drift": no_drift(),
        "performance": good_performance(),
        "decision": decide(healthy_quality(), no_drift(), good_performance()),
        "model_version": 7,
        "git_sha": SHA,
    }
    defaults.update(overrides)
    return build_alert_rows(**defaults)  # type: ignore[arg-type]


def test_a_healthy_run_raises_no_alerts() -> None:
    assert alerts() == []


def test_alert_payload_shape() -> None:
    raised = alerts(
        drift=bad_drift(), decision=decide(healthy_quality(), bad_drift(), good_performance())
    )

    assert len(raised) == 1
    assert set(raised[0]) == {
        "raised_at",
        "severity",
        "alert_type",
        "message",
        "model_version",
        "git_sha",
        "acknowledged",
    }


def test_alerts_are_never_written_pre_acknowledged() -> None:
    """Acknowledgement is a human act recorded later."""
    quality = check_data_quality(batch_stats(rows=0), prediction_stats(), actuals_stats())

    for alert in alerts(quality=quality, decision=decide(quality, no_drift(), good_performance())):
        assert alert["acknowledged"] is False


def test_drift_raises_a_warning_alert() -> None:
    raised = alerts(
        drift=bad_drift(), decision=decide(healthy_quality(), bad_drift(), good_performance())
    )

    assert raised[0]["severity"] == SEVERITY_WARNING
    assert raised[0]["alert_type"] == ALERT_DRIFT


def test_performance_raises_a_warning_alert_per_failure() -> None:
    perf = bad_performance()
    raised = alerts(performance=perf, decision=decide(healthy_quality(), no_drift(), perf))

    assert len(raised) == len(perf.failures) == 2
    assert all(a["alert_type"] == ALERT_PERFORMANCE for a in raised)
    assert all(a["severity"] == SEVERITY_WARNING for a in raised)


def test_data_quality_raises_critical_alerts() -> None:
    quality = check_data_quality(batch_stats(rows=0), prediction_stats(), actuals_stats())
    raised = alerts(quality=quality, decision=decide(quality, no_drift(), good_performance()))

    assert raised
    assert all(a["severity"] == SEVERITY_CRITICAL for a in raised)
    assert all(a["alert_type"] == ALERT_DATA_QUALITY for a in raised)


def test_a_data_quality_fault_suppresses_drift_and_performance_alerts() -> None:
    """Metrics computed on data just declared untrustworthy are noise, not signal."""
    quality = check_data_quality(batch_stats(rows=0), prediction_stats(), actuals_stats())
    raised = alerts(
        quality=quality,
        drift=bad_drift(),
        performance=bad_performance(),
        decision=decide(quality, bad_drift(), bad_performance()),
    )

    assert all(a["alert_type"] == ALERT_DATA_QUALITY for a in raised)


def test_forced_retraining_creates_an_audit_alert() -> None:
    forced = decide(healthy_quality(), no_drift(), good_performance(), force_retrain=True)
    raised = alerts(decision=forced)

    assert len(raised) == 1
    assert raised[0]["alert_type"] == ALERT_FORCED_RETRAIN
    assert raised[0]["severity"] == SEVERITY_INFO
    assert raised[0]["model_version"] == 7
    assert raised[0]["git_sha"] == SHA
