"""Monitoring task — observe the deployed Champion and decide about retraining.

Reads. Measures. Records. Decides. It moves no alias, registers no model and
writes no registry metadata: monitoring that could also change what it monitors
would make the two indistinguishable in an incident.

Spark computes the aggregates here; every decision comes from monitoring_logic,
which is pure and unit-tested. The split is deliberate — the numbers need a
cluster, the judgements do not, and only the judgements are worth arguing about.

Outputs, in order of durability:

  1. one appended monitoring_history row, written whatever the verdict;
  2. zero or more appended monitoring_alerts rows;
  3. a Databricks task value `retraining_required`, which the job's condition
     task branches on;
  4. a non-zero exit when data quality is CRITICAL, which fails the job and is
     visible in the Databricks jobs UI.

For Phase 15 those four ARE the alerting boundary. External routing — email,
Teams, PagerDuty — belongs to the later production-operations phase.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime

import mlflow
from mlflow.tracking import MlflowClient
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from ml_logic import (
    CHAMPION_ALIAS,
    FEATURE_COLUMNS,
    ID_COLUMN,
    LABEL_COLUMN,
    PREDICTION_COLUMN,
    ProductConfig,
    parse_model_version,
    resolve_config,
)
from monitoring_logic import (
    FeatureStats,
    TableStats,
    build_alert_rows,
    build_history_row,
    check_data_quality,
    compute_drift,
    decide,
    evaluate_performance,
)

TASK_VALUE_KEY = "retraining_required"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor the deployed ml-lifecycle-demo model.")
    parser.add_argument(
        "--force-retrain",
        default="false",
        help=(
            "When 'true', request retraining regardless of drift and performance. "
            "Supplied by the job parameter of the same name; exists for an explicit, "
            "audited proof of the retraining path."
        ),
    )

    return parser.parse_args()


def _is_true(value: str) -> bool:
    """Parse a Databricks job parameter, which always arrives as a string."""
    return value.strip().lower() in {"true", "1", "yes"}


def _table_stats(spark: SparkSession, table: str) -> TableStats:
    """Row count, columns and per-column null counts for one table."""
    frame = spark.table(table)
    columns = tuple(frame.columns)

    # One pass for the count and one aggregate for the nulls, rather than a
    # scan per column: the tables are small now, but a per-column scan is the
    # kind of thing that quietly becomes the reason monitoring gets disabled.
    row_count: int = frame.count()

    null_counts: dict[str, int] = {}
    if row_count:
        totals = frame.select(
            *[F.sum(F.col(name).isNull().cast("long")).alias(name) for name in columns]
        ).collect()[0]
        null_counts = {name: int(totals[name] or 0) for name in columns}

    return TableStats(
        name=table,
        row_count=row_count,
        columns=columns,
        null_counts=null_counts,
    )


def _feature_stats(frame: DataFrame) -> dict[str, FeatureStats]:
    """Mean and (population) standard deviation of each feature column."""
    aggregates = frame.select(
        *[F.mean(F.col(name)).alias(f"{name}__mean") for name in FEATURE_COLUMNS],
        *[F.stddev_pop(F.col(name)).alias(f"{name}__std") for name in FEATURE_COLUMNS],
    ).collect()[0]

    return {
        name: FeatureStats(
            mean=float(aggregates[f"{name}__mean"] or 0.0),
            stddev=float(aggregates[f"{name}__std"] or 0.0),
        )
        for name in FEATURE_COLUMNS
    }


def _live_performance(spark: SparkSession, config: ProductConfig) -> tuple[float, float, int]:
    """RMSE and R2 of the predictions against delayed actuals, joined by row_id.

    An inner join, so rows whose ground truth has not arrived yet are simply not
    scored rather than counted as errors.
    """
    predictions = spark.table(config.predictions_table).select(ID_COLUMN, PREDICTION_COLUMN)
    actuals = spark.table(config.inference_actuals_table).select(ID_COLUMN, LABEL_COLUMN)

    joined = predictions.join(actuals, on=ID_COLUMN, how="inner")
    matched: int = joined.count()

    if matched == 0:
        # No ground truth yet. Reported as a neutral, passing result: absence of
        # evidence is not evidence of a bad model, and the row counts recorded
        # in monitoring_history say plainly that nothing was scored.
        return 0.0, 1.0, 0

    stats = joined.select(
        F.sqrt(F.mean(F.pow(F.col(PREDICTION_COLUMN) - F.col(LABEL_COLUMN), 2))).alias("rmse"),
        F.var_pop(F.col(LABEL_COLUMN)).alias("label_variance"),
        F.mean(F.pow(F.col(PREDICTION_COLUMN) - F.col(LABEL_COLUMN), 2)).alias("mse"),
    ).collect()[0]

    rmse = float(stats["rmse"] or 0.0)
    variance = float(stats["label_variance"] or 0.0)
    mse = float(stats["mse"] or 0.0)

    # R2 = 1 - MSE/Var. Constant actuals have zero variance and no explainable
    # signal, so R2 is undefined; reported as 1.0 (nothing to explain, nothing
    # explained badly) rather than as a division error.
    r2 = 1.0 if variance == 0.0 else 1.0 - (mse / variance)

    return rmse, r2, matched


def _publish_task_value(key: str, value: str) -> None:
    """Publish a task value for the job's condition task to branch on.

    Imported lazily and guarded: dbutils exists only inside a Databricks job, and
    a monitor run outside one should still produce its history and alert rows
    rather than crash on an import.
    """
    try:
        from databricks.sdk.runtime import dbutils

        dbutils.jobs.taskValues.set(key=key, value=value)
        print(f"Published task value {key}={value}")
    except Exception as error:  # noqa: BLE001 - absence of dbutils must not lose the record
        print(f"WARNING: could not publish task value {key}={value}: {error}")


def _append(spark: SparkSession, rows: list[dict[str, object]], table: str) -> None:
    """Append rows to a governed Delta table, creating it on first write."""
    if not rows:
        print(f"No rows to append to {table}")
        return

    spark.createDataFrame(rows).write.mode("append").option("mergeSchema", "true").saveAsTable(
        table
    )

    print(f"Appended {len(rows)} row(s) to {table}")


def main() -> None:
    args = _parse_args()
    force_retrain = _is_true(args.force_retrain)

    config = resolve_config()
    spark = SparkSession.builder.getOrCreate()

    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")

    # Read-only. Monitoring observes which version is Champion; it never moves it.
    champion = MlflowClient().get_model_version_by_alias(config.model_name, CHAMPION_ALIAS)
    version = parse_model_version(champion.version)

    observed_at = datetime.now(UTC).isoformat()
    git_sha = os.environ.get("GIT_SHA", "").strip() or None

    print(f"Monitoring {config.model_name} version {version} in {config.catalog}")
    print(f"force_retrain={force_retrain}")

    batch_stats = _table_stats(spark, config.batch_input_table)
    prediction_stats = _table_stats(spark, config.predictions_table)
    actuals_stats = _table_stats(spark, config.inference_actuals_table)

    quality = check_data_quality(batch_stats, prediction_stats, actuals_stats)

    drift = compute_drift(
        _feature_stats(spark.table(config.training_table)),
        _feature_stats(spark.table(config.batch_input_table)),
    )

    rmse, r2, matched = _live_performance(spark, config)
    performance = evaluate_performance(rmse, r2)

    print(f"Data quality: passed={quality.passed} null_rate={quality.null_rate:.6f}")
    print(f"Drift: max={drift.max_drift:.6f} per_feature={dict(drift.per_feature)}")
    print(f"Performance: rmse={rmse:.4f} r2={r2:.4f} matched_rows={matched}")

    decision = decide(quality, drift, performance, force_retrain=force_retrain)

    for reason in decision.reasons:
        print(f"  reason: {reason}")

    # Record BEFORE deciding what to do about it. If the raise below fails the
    # task, the evidence of why is already durable.
    _append(
        spark,
        [
            build_history_row(
                observed_at=observed_at,
                environment=config.catalog,
                model_name=config.model_name,
                model_version=version,
                quality=quality,
                drift=drift,
                performance=performance,
                decision=decision,
                git_sha=git_sha,
            )
        ],
        config.monitoring_history_table,
    )

    _append(
        spark,
        build_alert_rows(
            observed_at=observed_at,
            quality=quality,
            drift=drift,
            performance=performance,
            decision=decision,
            model_version=version,
            git_sha=git_sha,
        ),
        config.monitoring_alerts_table,
    )

    _publish_task_value(TASK_VALUE_KEY, "true" if decision.retraining_required else "false")

    if decision.is_critical:
        raise RuntimeError(
            "Monitoring found CRITICAL data-quality failures: "
            + "; ".join(decision.reasons)
            + ". No retraining was requested — retraining on broken data would fit "
            "a model to the breakage."
        )

    print(
        f"Monitoring status: {decision.status} (retraining_required={decision.retraining_required})"
    )


if __name__ == "__main__":
    main()
