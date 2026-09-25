"""Environment-neutral monitoring logic for ml-lifecycle-demo.

Pure Python: no Spark session, no MLflow client, no Databricks SDK, no
credential. Spark computes the aggregates — row counts, null counts, feature
means and standard deviations, joined RMSE and R2 — and hands them here as
plain numbers. Every DECISION lives in this module, which is why the whole
monitoring contract is testable on a laptop in milliseconds.

The decisions are deliberately boring and explainable. Monitoring that nobody
can reason about at 3am gets muted, and a muted monitor is worse than none.

Thresholds are version-controlled here rather than configured per environment,
for the same reason the training gate is: a model that is drifting in PROD is
drifting in DEV too, and per-environment thresholds would let a problem pass the
early gates and surface late.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ml_logic import FEATURE_COLUMNS, ID_COLUMN, LABEL_COLUMN, PREDICTION_COLUMN

# --- Statuses and severities ------------------------------------------------

STATUS_OK = "ok"
STATUS_RETRAIN = "retrain_requested"
STATUS_CRITICAL = "critical"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

ALERT_DATA_QUALITY = "data_quality"
ALERT_DRIFT = "feature_drift"
ALERT_PERFORMANCE = "model_performance"
ALERT_FORCED_RETRAIN = "forced_retrain"

# --- Version-controlled thresholds ------------------------------------------

#: Fraction of null cells tolerated across a monitored table. Above this the
#: data is broken, not merely degraded.
MAX_NULL_RATE = 0.01

#: Standardized mean difference above which a feature counts as drifted.
#:
#: SMD = |mean(batch) - mean(train)| / stddev(train), i.e. how far the batch
#: mean has moved expressed in training standard deviations. Chosen over a
#: KS test or PSI precisely because it is explainable: "feature_1's average has
#: moved 0.4 training standard deviations" is a sentence an on-call engineer can
#: act on. 0.2 is the conventional "small effect" boundary.
MAX_FEATURE_DRIFT = 0.2

#: Live performance bounds, measured against delayed ground truth. Looser than
#: the training gate (MAX_RMSE 15.0 / MIN_R2 0.90) on purpose: the training gate
#: judges a fresh fit on held-out data, while these judge a deployed model on
#: data that has moved on. Tripping these requests a retrain; it is not a fault.
MONITOR_MAX_RMSE = 18.0
MONITOR_MIN_R2 = 0.85

#: Columns each monitored table must carry for monitoring to mean anything.
REQUIRED_BATCH_INPUT_COLUMNS = (ID_COLUMN, *FEATURE_COLUMNS)
REQUIRED_PREDICTIONS_COLUMNS = (ID_COLUMN, PREDICTION_COLUMN)
REQUIRED_ACTUALS_COLUMNS = (ID_COLUMN, LABEL_COLUMN)


@dataclass(frozen=True)
class TableStats:
    """Spark-computed shape of one monitored table."""

    name: str
    row_count: int
    columns: tuple[str, ...]
    null_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def null_rate(self) -> float:
        """Fraction of null cells across the whole table.

        Zero rows means zero cells, and an empty table is reported as empty by
        the row-count check rather than as a division error here.
        """
        cells = self.row_count * len(self.columns)

        if cells == 0:
            return 0.0

        return sum(self.null_counts.values()) / cells

    def missing_columns(self, required: Sequence[str]) -> tuple[str, ...]:
        return tuple(column for column in required if column not in self.columns)


@dataclass(frozen=True)
class FeatureStats:
    """Spark-computed mean and standard deviation of one feature."""

    mean: float
    stddev: float


@dataclass(frozen=True)
class DataQualityResult:
    failures: tuple[str, ...]
    null_rate: float
    batch_row_count: int
    prediction_row_count: int
    actuals_row_count: int

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class DriftResult:
    per_feature: Mapping[str, float]
    max_drift: float
    drifted_features: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.drifted_features


@dataclass(frozen=True)
class PerformanceResult:
    rmse: float
    r2: float
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class MonitoringDecision:
    """The whole verdict of one monitor run."""

    status: str
    retraining_required: bool
    reasons: tuple[str, ...]
    forced: bool = False

    @property
    def is_critical(self) -> bool:
        return self.status == STATUS_CRITICAL


# --- Data quality -----------------------------------------------------------


def check_data_quality(
    batch_input: TableStats,
    predictions: TableStats,
    actuals: TableStats,
) -> DataQualityResult:
    """Structural checks that must hold before any metric is worth computing.

    Every failure here is CRITICAL. A missing column, an empty table, or a
    prediction count that disagrees with the batch means the pipeline is broken,
    and a drift or performance number computed on top of it would be a
    confidently wrong answer.
    """
    failures: list[str] = []

    for stats, required in (
        (batch_input, REQUIRED_BATCH_INPUT_COLUMNS),
        (predictions, REQUIRED_PREDICTIONS_COLUMNS),
        (actuals, REQUIRED_ACTUALS_COLUMNS),
    ):
        missing = stats.missing_columns(required)
        if missing:
            failures.append(f"{stats.name} is missing required columns: {', '.join(missing)}")

        if stats.row_count == 0:
            failures.append(f"{stats.name} is empty")

        if stats.null_rate > MAX_NULL_RATE:
            failures.append(f"{stats.name} null rate {stats.null_rate:.4f} exceeds {MAX_NULL_RATE}")

    # Scored-row conservation. Anything other than exact equality means rows
    # were dropped or duplicated between reading the batch and writing the
    # predictions, which no downstream metric would reveal.
    if batch_input.row_count != predictions.row_count:
        failures.append(
            f"prediction count {predictions.row_count} does not equal "
            f"batch input count {batch_input.row_count}"
        )

    worst_null_rate = max(
        (stats.null_rate for stats in (batch_input, predictions, actuals)),
        default=0.0,
    )

    return DataQualityResult(
        failures=tuple(failures),
        null_rate=worst_null_rate,
        batch_row_count=batch_input.row_count,
        prediction_row_count=predictions.row_count,
        actuals_row_count=actuals.row_count,
    )


# --- Feature drift ----------------------------------------------------------


def standardized_mean_difference(training: FeatureStats, batch: FeatureStats) -> float:
    """|mean(batch) - mean(training)| / stddev(training).

    A constant training feature (stddev 0) has no scale to standardise against.
    Any movement at all is then infinitely many standard deviations, so it is
    reported as drift when the means differ and as none when they do not,
    rather than as a division error.
    """
    shift = abs(batch.mean - training.mean)

    if training.stddev == 0.0:
        return 0.0 if shift == 0.0 else math.inf

    return shift / training.stddev


def compute_drift(
    training: Mapping[str, FeatureStats],
    batch: Mapping[str, FeatureStats],
    features: Sequence[str] = FEATURE_COLUMNS,
) -> DriftResult:
    """Per-feature SMD and the maximum across features.

    The maximum, not the mean: one badly drifted feature is a real problem, and
    averaging it against three stable ones would hide it.
    """
    per_feature: dict[str, float] = {}
    drifted: list[str] = []

    for name in features:
        if name not in training or name not in batch:
            raise KeyError(f"missing feature statistics for '{name}'")

        score = standardized_mean_difference(training[name], batch[name])
        per_feature[name] = score

        if score > MAX_FEATURE_DRIFT:
            drifted.append(name)

    return DriftResult(
        per_feature=per_feature,
        max_drift=max(per_feature.values(), default=0.0),
        drifted_features=tuple(drifted),
    )


# --- Model performance ------------------------------------------------------


def evaluate_performance(rmse: float, r2: float) -> PerformanceResult:
    """Judge live performance against the version-controlled monitor bounds."""
    failures: list[str] = []

    if rmse > MONITOR_MAX_RMSE:
        failures.append(f"live RMSE {rmse:.4f} exceeds {MONITOR_MAX_RMSE}")

    if r2 < MONITOR_MIN_R2:
        failures.append(f"live R2 {r2:.4f} is below {MONITOR_MIN_R2}")

    return PerformanceResult(rmse=rmse, r2=r2, failures=tuple(failures))


# --- The decision -----------------------------------------------------------


def decide(
    quality: DataQualityResult,
    drift: DriftResult,
    performance: PerformanceResult,
    force_retrain: bool = False,
) -> MonitoringDecision:
    """Turn the three checks into one verdict.

    Ordering matters and is deliberate:

      1. CRITICAL data quality fails the job and requests NO retraining.
         Retraining on data known to be broken would burn compute to produce a
         candidate fitted to the breakage, and would launder a pipeline fault
         into a model everyone then has to reason about.
      2. Otherwise a forced retrain wins, because it exists to be unconditional.
      3. Otherwise drift or performance breaches request a retrain — a request,
         not a failure: the model still works, it is just no longer the best
         available fit.
    """
    if not quality.passed:
        return MonitoringDecision(
            status=STATUS_CRITICAL,
            retraining_required=False,
            reasons=quality.failures,
        )

    if force_retrain:
        return MonitoringDecision(
            status=STATUS_RETRAIN,
            retraining_required=True,
            reasons=("retraining was manually forced via force_retrain=true",),
            forced=True,
        )

    reasons: list[str] = []

    if not drift.passed:
        reasons.append(
            f"feature drift {drift.max_drift:.4f} exceeds {MAX_FEATURE_DRIFT} "
            f"({', '.join(drift.drifted_features)})"
        )

    reasons.extend(performance.failures)

    if reasons:
        return MonitoringDecision(
            status=STATUS_RETRAIN,
            retraining_required=True,
            reasons=tuple(reasons),
        )

    return MonitoringDecision(status=STATUS_OK, retraining_required=False, reasons=())


# --- Durable records --------------------------------------------------------


def build_history_row(
    *,
    observed_at: str,
    environment: str,
    model_name: str,
    model_version: int,
    quality: DataQualityResult,
    drift: DriftResult,
    performance: PerformanceResult,
    decision: MonitoringDecision,
    git_sha: str | None = None,
) -> dict[str, object]:
    """One append-only row describing this monitor run.

    Written whatever the verdict, including a clean one: a monitoring table that
    only records problems cannot answer "when did this start?".
    """
    return {
        "observed_at": observed_at,
        "environment": environment,
        "model_name": model_name,
        "model_version": model_version,
        "batch_row_count": quality.batch_row_count,
        "prediction_row_count": quality.prediction_row_count,
        "actuals_row_count": quality.actuals_row_count,
        "null_rate": round(quality.null_rate, 6),
        "drift_score": None if math.isinf(drift.max_drift) else round(drift.max_drift, 6),
        "rmse": round(performance.rmse, 6),
        "r2": round(performance.r2, 6),
        "status": decision.status,
        "retraining_required": decision.retraining_required,
        "retraining_forced": decision.forced,
        "reasons": "; ".join(decision.reasons),
        "git_sha": git_sha or "",
    }


def build_alert_rows(
    *,
    observed_at: str,
    quality: DataQualityResult,
    drift: DriftResult,
    performance: PerformanceResult,
    decision: MonitoringDecision,
    model_version: int,
    git_sha: str | None = None,
) -> list[dict[str, object]]:
    """Zero or more append-only alert rows.

    acknowledged is always false on write: acknowledgement is a human act
    recorded later, and a self-acknowledging alert would be pointless.

    For Phase 15 these rows ARE the alert channel. External routing — email,
    Teams, PagerDuty — belongs to the later production-operations phase; see
    docs/platform/ml-operations-runbook.md.
    """

    def row(severity: str, alert_type: str, message: str) -> dict[str, object]:
        return {
            "raised_at": observed_at,
            "severity": severity,
            "alert_type": alert_type,
            "message": message,
            "model_version": model_version,
            "git_sha": git_sha or "",
            "acknowledged": False,
        }

    alerts: list[dict[str, object]] = []

    for failure in quality.failures:
        alerts.append(row(SEVERITY_CRITICAL, ALERT_DATA_QUALITY, failure))

    # Drift and performance alerts are suppressed when data quality already
    # failed: the metrics they describe were computed on data the previous
    # checks just declared untrustworthy, so raising them would be noise on top
    # of a real fault.
    if quality.passed:
        if decision.forced:
            alerts.append(
                row(
                    SEVERITY_INFO,
                    ALERT_FORCED_RETRAIN,
                    "Retraining manually forced via force_retrain=true; "
                    "candidate training and validation proceed unchanged.",
                )
            )

        if not drift.passed:
            alerts.append(
                row(
                    SEVERITY_WARNING,
                    ALERT_DRIFT,
                    f"Maximum feature drift {drift.max_drift:.4f} exceeds "
                    f"{MAX_FEATURE_DRIFT} ({', '.join(drift.drifted_features)}).",
                )
            )

        for failure in performance.failures:
            alerts.append(row(SEVERITY_WARNING, ALERT_PERFORMANCE, failure))

    return alerts
