"""Task 1 — materialise the governed training, batch-input and actuals tables.

Writes three Unity Catalog tables from the deterministic generator in ml_logic.
The write is a full overwrite and therefore idempotent: re-running the job
reproduces exactly the same rows rather than accumulating them, which is what
makes the downstream training metrics comparable run to run.

inference_actuals is the DELAYED GROUND TRUTH for the batch rows: the labels
that, in a real system, only arrive after the predictions were made. It is
written here for the monitoring job to join against later, and it carries
row_id and label only — no features — so it cannot be mistaken for, or joined
into, an inference input. NOTHING in the scoring path reads it.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import DoubleType, StructField, StructType

from ml_logic import (
    FEATURE_COLUMNS,
    ID_COLUMN,
    LABEL_COLUMN,
    actuals_records,
    batch_input_records,
    resolve_config,
    training_records,
)

_FEATURE_FIELDS = [StructField(name, DoubleType(), nullable=False) for name in FEATURE_COLUMNS]

TRAINING_SCHEMA = StructType(
    [
        StructField(ID_COLUMN, DoubleType(), nullable=False),
        *_FEATURE_FIELDS,
        StructField(LABEL_COLUMN, DoubleType(), nullable=False),
    ]
)

BATCH_INPUT_SCHEMA = StructType(
    [
        StructField(ID_COLUMN, DoubleType(), nullable=False),
        *_FEATURE_FIELDS,
    ]
)

# row_id and label only. The absence of feature columns here is the structural
# guarantee that the scoring path cannot read its own answers.
ACTUALS_SCHEMA = StructType(
    [
        StructField(ID_COLUMN, DoubleType(), nullable=False),
        StructField(LABEL_COLUMN, DoubleType(), nullable=False),
    ]
)


def _write(
    spark: SparkSession,
    records: list[dict[str, float]],
    schema: StructType,
    table: str,
) -> DataFrame:
    # Explicit schema rather than inference: the table's column types are part of
    # the contract the training and inference tasks rely on, and inference from a
    # list of dicts is order- and content-dependent.
    frame = spark.createDataFrame(records, schema=schema)
    frame.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table)

    print(f"Wrote {frame.count()} rows to {table}")

    return frame


def main() -> None:
    config = resolve_config()
    spark = SparkSession.builder.getOrCreate()

    print(f"Preparing data in {config.catalog}.{config.schema}")

    _write(spark, training_records(), TRAINING_SCHEMA, config.training_table)
    _write(spark, batch_input_records(), BATCH_INPUT_SCHEMA, config.batch_input_table)
    _write(spark, actuals_records(), ACTUALS_SCHEMA, config.inference_actuals_table)


if __name__ == "__main__":
    main()
