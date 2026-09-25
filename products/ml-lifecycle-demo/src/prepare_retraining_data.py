"""Retraining task 1 — assemble a training set that contains something new.

The point of controlled retraining is to learn from observations the current
Champion never saw. Refitting the unchanged training_data table would produce a
near-identical model, burn a cluster, and register a candidate that answers a
question nobody asked — while the drift that triggered the retrain went
unaddressed.

So this task joins the batch features to the ground truth that has since
arrived, combines those newly labelled observations with the original training
set, and writes retraining_data. train_register then fits on THAT.

training_data is never modified. It stays the stable, reproducible baseline that
cross-environment metric comparison depends on.
"""

from __future__ import annotations

from pyspark.sql import SparkSession

from ml_logic import (
    FEATURE_COLUMNS,
    ID_COLUMN,
    LABEL_COLUMN,
    ProductConfig,
    combine_training_sets,
    join_labelled_observations,
    resolve_config,
)
from prepare_data import TRAINING_SCHEMA


def _records(spark: SparkSession, table: str, columns: list[str]) -> list[dict[str, float]]:
    """Read a bounded governed table into plain records.

    collect() is safe here by construction: these tables hold TRAINING_ROWS and
    BATCH_ROWS rows, a few hundred in total. See the note in ml_logic on when
    this would need to become a Spark join.
    """
    rows = spark.table(table).select(*columns).collect()

    return [{column: float(row[column]) for column in columns} for row in rows]


def build(spark: SparkSession, config: ProductConfig) -> tuple[int, int, int]:
    """Assemble the retraining set. Returns (original, newly labelled, final)."""
    feature_columns = [ID_COLUMN, *FEATURE_COLUMNS]

    original = _records(spark, config.training_table, [*feature_columns, LABEL_COLUMN])
    batch_features = _records(spark, config.batch_input_table, feature_columns)
    actuals = _records(spark, config.inference_actuals_table, [ID_COLUMN, LABEL_COLUMN])

    # Fails closed on an empty join, duplicate row_ids, or any batch row whose
    # label has not arrived.
    newly_labelled = join_labelled_observations(batch_features, actuals)

    combined = combine_training_sets(original, newly_labelled)

    # Idempotent overwrite with the SAME schema training_data uses, so the two
    # tables stay interchangeable as far as train_register is concerned.
    spark.createDataFrame(combined, schema=TRAINING_SCHEMA).write.mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(config.retraining_data_table)

    return len(original), len(newly_labelled), len(combined)


def main() -> None:
    config = resolve_config()
    spark = SparkSession.builder.getOrCreate()

    print(f"Assembling retraining data in {config.catalog}.{config.schema}")

    original, newly_labelled, final = build(spark, config)

    print(f"  original training rows:      {original}")
    print(f"  newly labelled observations: {newly_labelled}")
    print(f"  final retraining rows:       {final}")
    print(f"Wrote {final} rows to {config.retraining_data_table}")

    if final <= original:
        # Not fatal: a rerun before new ground truth arrives legitimately
        # produces the same set. Worth saying out loud, because a retrain that
        # added nothing is a retrain that will change nothing.
        print(
            "NOTE: the retraining set is no larger than the original. "
            "This retrain will fit essentially the same data."
        )


if __name__ == "__main__":
    main()
