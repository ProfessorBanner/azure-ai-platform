"""Task 3 — prove the Candidate scores, before anyone is asked to approve it.

Runs in every environment as the last task of the ml_candidate job. It resolves
Candidate ONCE, pins the resulting version number, and does everything else
against that exact number.

Why the pinning matters: an alias is a mutable pointer. Between resolving
`@Candidate` and loading it, a concurrent training run could move the alias, and
this task would then validate an artefact that is not the one it reported. The
job sets max_concurrent_runs: 1 to make that unlikely; resolving once and
pinning the integer makes it impossible to go unnoticed.

Output goes to candidate_predictions, NOT predictions. A candidate is an
unapproved model; letting it write the table production consumers read would
make validating a candidate and shipping it the same act.
"""

from __future__ import annotations

import os

import mlflow
from mlflow.tracking import MlflowClient
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from ml_logic import (
    CANDIDATE_ALIAS,
    FEATURE_COLUMNS,
    PREDICTION_COLUMN,
    ProductConfig,
    parse_model_version,
    resolve_config,
    verify_model_version_provenance,
)


def _model_version_tags(
    client: MlflowClient, config: ProductConfig, version: int
) -> dict[str, str]:
    """Tags on one exact model version, as a plain dict."""
    details = client.get_model_version(name=config.model_name, version=str(version))

    return dict(details.tags or {})


def score(
    spark: SparkSession,
    config: ProductConfig,
    model_uri: str,
    version: int,
    target_table: str,
) -> int:
    """Score batch_input with one exact model version and persist the result."""
    predict = mlflow.pyfunc.spark_udf(spark, model_uri=model_uri)

    batch_input = spark.table(config.batch_input_table)

    # One struct argument: the model's signature is named scalar columns, and a
    # struct is how Spark hands MLflow named fields.
    predictions = batch_input.withColumn(
        PREDICTION_COLUMN,
        predict(F.struct(*[F.col(name) for name in FEATURE_COLUMNS])),
    ).withColumns(
        {
            "model_name": F.lit(config.model_name),
            "model_version": F.lit(str(version)),
            "scored_at": F.current_timestamp(),
        }
    )

    predictions.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(target_table)

    # Count what actually LANDED in the governed table, not what was submitted.
    written = spark.table(target_table)
    row_count: int = written.count()

    if row_count == 0:
        raise RuntimeError(f"{target_table} is empty after scoring — no predictions written.")

    print(f"Wrote {row_count} predictions to {target_table}")
    written.show(5, truncate=False)

    return row_count


def main() -> None:
    config = resolve_config()
    spark = SparkSession.builder.getOrCreate()

    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")

    client = MlflowClient()

    # RESOLVE ONCE. Everything downstream uses `version`, never the alias.
    candidate = client.get_model_version_by_alias(config.model_name, CANDIDATE_ALIAS)
    version = parse_model_version(candidate.version)

    print(f"Candidate resolved to {config.model_name} version {version}")

    # Provenance, from the tags training wrote onto the version itself. GIT_SHA
    # is checked only when the deployment supplied one — outside a Git checkout
    # there is no commit to compare against, and demanding one would block a
    # legitimate interactive run. When it IS supplied, a missing tag fails.
    verify_model_version_provenance(
        _model_version_tags(client, config, version),
        expected_environment=config.catalog,
        expected_git_sha=os.environ.get("GIT_SHA", "").strip() or None,
    )

    print(f"Provenance verified for version {version} (environment={config.catalog})")

    score(
        spark,
        config,
        model_uri=config.version_model_uri(version),
        version=version,
        target_table=config.candidate_predictions_table,
    )

    print(f"Candidate version {version} validated. Champion is unchanged.")


if __name__ == "__main__":
    main()
