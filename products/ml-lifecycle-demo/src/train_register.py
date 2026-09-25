"""Task 2 — train, evaluate, gate, register a CANDIDATE.

The governed order of operations is the point of the task:

    train -> evaluate -> ENFORCE THRESHOLD -> register -> tag -> assign Candidate

The threshold is enforced BEFORE registration, not after. A model that cannot
clear the bar never becomes a registered version and never touches an alias, so
the registry only ever contains versions that passed their gate. The
alternative — register first, then fail — leaves failed versions in a governed
catalog for someone to mistake for a candidate later.

THIS TASK NEVER TOUCHES CHAMPION. It produces a candidate and says so, in tags
that travel with the artefact. Who becomes Champion is a promotion decision made
outside this code: automatically in DEV and STG, and only after an explicit
human approval of one exact numbered version in PROD. Training that promoted
itself would make that approval decorative.

The MLflow experiment is NOT created here. It is a Bundle resource, deployed and
owned by the same governed pipeline as the job, and its ID is passed in as a
task parameter. Source code that creates its own experiments produces a
workspace nobody can reason about.
"""

from __future__ import annotations

import argparse

import mlflow
import numpy as np
import pandas as pd
from mlflow.models import infer_signature
from mlflow.tracking import MlflowClient
from pyspark.sql import SparkSession
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split

from ml_logic import (
    ALLOWED_TRAINING_TABLES,
    CANDIDATE_ALIAS,
    FEATURE_COLUMNS,
    LABEL_COLUMN,
    RANDOM_STATE,
    TEST_FRACTION,
    build_model_version_tags,
    build_run_tags,
    enforce_threshold,
    evaluate_predictions,
    resolve_config,
    select_training_table,
)

MODEL_ARTIFACT_NAME = "model"


def _feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Features as a NAMED-COLUMN DataFrame in canonical FEATURE_COLUMNS order.

    The model is fitted, evaluated, signed and exemplified through this one
    representation, deliberately. Handing scikit-learn a bare NumPy array works,
    but it makes the logged MLflow signature an anonymous
    ``Tensor('float64', (-1, 3))`` — a shape with no field names. Spark
    inference then has nothing to match columns against: mlflow.pyfunc.spark_udf
    can only feed such a model positionally, and the batch table's column names
    stop meaning anything. Column ORDER silently becomes the contract, and a
    reordered SELECT scores wrong rather than failing.

    A named DataFrame instead yields a signature of named scalar columns, which
    is a contract MLflow can enforce, and which batch_inference satisfies by
    passing a struct of the same names.
    """
    return frame[list(FEATURE_COLUMNS)].astype("float64")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train and register the ml-lifecycle-demo model.")
    parser.add_argument(
        "--experiment-id",
        required=True,
        help="ID of the Bundle-managed MLflow experiment this run belongs to.",
    )
    parser.add_argument(
        "--training-table",
        default=None,
        help=(
            "Short name of the table to fit on: one of "
            f"{', '.join(ALLOWED_TRAINING_TABLES)}. Omitted by the ordinary candidate "
            "job, which uses the governed default; retraining passes retraining_data. "
            "A short name, never a qualified one — the catalog and schema always come "
            "from the environment, so this cannot reach another namespace."
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = resolve_config()

    spark = SparkSession.builder.getOrCreate()

    # Unity Catalog is the model registry. Without this the three-level model
    # name would be rejected by the workspace registry.
    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")

    # Fails closed on anything outside the allowlist, and qualifies the name
    # with THIS environment's catalog and schema.
    training_table = select_training_table(config, args.training_table)

    training = spark.table(training_table).toPandas()
    print(f"Loaded {len(training)} training rows from {training_table}")

    features = _feature_frame(training)
    labels = training[LABEL_COLUMN].to_numpy(dtype=np.float64)

    x_train, x_test, y_train, y_test = train_test_split(
        features,
        labels,
        test_size=TEST_FRACTION,
        random_state=RANDOM_STATE,
    )

    model = LinearRegression()
    model.fit(x_train, y_train)

    result = evaluate_predictions(y_test, model.predict(x_test))
    print(f"Evaluation: rmse={result.rmse:.4f} r2={result.r2:.4f} passed={result.passed}")

    with mlflow.start_run(experiment_id=args.experiment_id) as run:
        mlflow.set_tags(build_run_tags(config))

        mlflow.log_params(
            {
                "model_type": "LinearRegression",
                "random_state": RANDOM_STATE,
                "test_fraction": TEST_FRACTION,
                "n_features": len(FEATURE_COLUMNS),
                "n_training_rows": int(len(x_train)),
                "n_test_rows": int(len(x_test)),
                # Recorded so a model version says which data it learned from.
                # Two candidates with different metrics are otherwise impossible
                # to tell apart after the fact.
                "training_table": training_table,
            }
        )
        mlflow.log_metrics(result.as_metrics())

        mlflow.sklearn.log_model(
            sk_model=model,
            name=MODEL_ARTIFACT_NAME,
            # Both the signature and the example are inferred from the NAMED
            # frame, so the logged contract is named scalar columns rather than
            # an anonymous tensor.
            signature=infer_signature(x_train, model.predict(x_train)),
            input_example=x_train.head(5),
        )

        run_id = run.info.run_id

    # The gate. Raising here fails the Databricks task, which fails the job and
    # the pipeline stage that ran it. The run above is still recorded, with its
    # failing metrics — the evidence of the rejection is kept, the artefact is
    # simply never promoted.
    enforce_threshold(result)

    version = mlflow.register_model(
        model_uri=f"runs:/{run_id}/{MODEL_ARTIFACT_NAME}",
        name=config.model_name,
    )
    print(f"Registered {config.model_name} version {version.version}")

    client = MlflowClient()

    # Model-version tags, not only run tags. These are what the promotion
    # pipelines fetch and verify before moving Champion: they travel with the
    # artefact rather than with an experiment, and they are set BEFORE the alias
    # so a version can never be visible as Candidate without its provenance.
    for key, value in build_model_version_tags(config, run_id, result).items():
        client.set_model_version_tag(
            name=config.model_name,
            version=version.version,
            key=key,
            value=value,
        )

    print(f"Tagged {config.model_name} version {version.version} as a candidate")

    client.set_registered_model_alias(
        name=config.model_name,
        alias=CANDIDATE_ALIAS,
        version=version.version,
    )
    print(f"Assigned alias {CANDIDATE_ALIAS} to {config.model_name} version {version.version}")
    print("Champion is unchanged by this task — promotion is a separate decision.")


if __name__ == "__main__":
    main()
