"""Unit tests for the ml-lifecycle-demo decision logic.

These tests deliberately require no Databricks workspace, no Azure credential
and no Spark session. Everything asserted here is a property of the product's
logic, and a test that needed a cluster to prove it would be run rarely enough
to stop protecting anything.
"""

from __future__ import annotations

from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
import pytest
from mlflow.models import infer_signature
from sklearn.linear_model import LinearRegression

from ml_logic import (
    BATCH_ROWS,
    CHAMPION_ALIAS,
    FEATURE_COLUMNS,
    ID_COLUMN,
    LABEL_COLUMN,
    MAX_RMSE,
    MIN_R2,
    PRODUCT_SCHEMA,
    RANDOM_STATE,
    TRAINING_ROWS,
    EvaluationResult,
    EvaluationThresholdError,
    ProductConfig,
    batch_input_records,
    build_records,
    enforce_threshold,
    evaluate_predictions,
    generate_regression_data,
    resolve_config,
    training_records,
)

DEV_ENV = {"CATALOG": "dev", "SCHEMA": PRODUCT_SCHEMA}


# --- Deterministic data generation ------------------------------------------


def test_generation_is_reproducible_across_calls() -> None:
    first_features, first_labels = generate_regression_data()
    second_features, second_labels = generate_regression_data()

    np.testing.assert_array_equal(first_features, second_features)
    np.testing.assert_array_equal(first_labels, second_labels)


def test_generation_shape_matches_declared_dataset_size() -> None:
    features, labels = generate_regression_data()

    assert features.shape == (TRAINING_ROWS + BATCH_ROWS, len(FEATURE_COLUMNS))
    assert labels.shape == (TRAINING_ROWS + BATCH_ROWS,)


def test_a_different_seed_produces_different_data() -> None:
    """Guards against a generator that ignores its seed and only looks stable."""
    baseline, _ = generate_regression_data()
    other, _ = generate_regression_data(random_state=RANDOM_STATE + 1)

    assert not np.array_equal(baseline, other)


def test_generation_rejects_a_non_positive_row_count() -> None:
    with pytest.raises(ValueError, match="n_rows must be positive"):
        generate_regression_data(n_rows=0)


def test_training_records_are_labelled_and_reproducible() -> None:
    records = training_records()

    assert len(records) == TRAINING_ROWS
    assert set(records[0]) == {ID_COLUMN, *FEATURE_COLUMNS, LABEL_COLUMN}
    assert records == training_records()


def test_batch_input_records_are_unlabelled() -> None:
    records = batch_input_records()

    assert len(records) == BATCH_ROWS
    assert set(records[0]) == {ID_COLUMN, *FEATURE_COLUMNS}
    assert LABEL_COLUMN not in records[0]


def test_batch_input_continues_the_training_id_range() -> None:
    """Training and batch rows are one dataset cut in two, not two datasets."""
    training = training_records()
    batch = batch_input_records()

    assert training[0][ID_COLUMN] == 0.0
    assert training[-1][ID_COLUMN] == float(TRAINING_ROWS - 1)
    assert batch[0][ID_COLUMN] == float(TRAINING_ROWS)

    training_ids = {record[ID_COLUMN] for record in training}
    batch_ids = {record[ID_COLUMN] for record in batch}
    assert training_ids.isdisjoint(batch_ids)


def test_batch_input_features_come_from_the_same_generated_dataset() -> None:
    features, _ = generate_regression_data()
    batch = batch_input_records()

    np.testing.assert_allclose(
        [[record[name] for name in FEATURE_COLUMNS] for record in batch],
        features[TRAINING_ROWS:],
    )


def test_build_records_rejects_mismatched_labels() -> None:
    features, labels = generate_regression_data(n_rows=10)

    with pytest.raises(ValueError, match="same length"):
        build_records(features, labels[:5])


# --- Model-name and table-name construction ---------------------------------


@pytest.mark.parametrize("catalog", ["dev", "stg", "prod"])
def test_model_name_is_three_level_and_environment_scoped(catalog: str) -> None:
    config = ProductConfig(catalog=catalog, schema=PRODUCT_SCHEMA)

    assert config.model_name == f"{catalog}.{PRODUCT_SCHEMA}.linear_regression_model"


def test_champion_uri_targets_the_alias_not_a_version() -> None:
    config = ProductConfig(catalog="prod", schema=PRODUCT_SCHEMA)

    assert config.champion_model_uri == (
        f"models:/prod.{PRODUCT_SCHEMA}.linear_regression_model@{CHAMPION_ALIAS}"
    )


def test_governed_tables_are_fully_qualified() -> None:
    config = ProductConfig(catalog="stg", schema=PRODUCT_SCHEMA)

    assert config.training_table == f"stg.{PRODUCT_SCHEMA}.training_data"
    assert config.batch_input_table == f"stg.{PRODUCT_SCHEMA}.batch_input"
    assert config.predictions_table == f"stg.{PRODUCT_SCHEMA}.predictions"


def test_table_rejects_an_empty_name() -> None:
    config = ProductConfig(catalog="dev", schema=PRODUCT_SCHEMA)

    with pytest.raises(ValueError, match="table_name must not be empty"):
        config.table("")


# --- Evaluation threshold ---------------------------------------------------


def test_a_model_inside_both_bounds_passes() -> None:
    result = EvaluationResult(rmse=MAX_RMSE - 1.0, r2=MIN_R2 + 0.01)

    assert result.passed
    enforce_threshold(result)


def test_the_threshold_boundary_is_inclusive() -> None:
    result = EvaluationResult(rmse=MAX_RMSE, r2=MIN_R2)

    assert result.passed
    enforce_threshold(result)


def test_excessive_rmse_fails_the_task() -> None:
    result = EvaluationResult(rmse=MAX_RMSE + 0.01, r2=MIN_R2 + 0.05)

    assert not result.passed

    with pytest.raises(EvaluationThresholdError, match="failed the promotion threshold"):
        enforce_threshold(result)


def test_insufficient_r2_fails_the_task() -> None:
    result = EvaluationResult(rmse=MAX_RMSE - 1.0, r2=MIN_R2 - 0.01)

    assert not result.passed

    with pytest.raises(EvaluationThresholdError, match="failed the promotion threshold"):
        enforce_threshold(result)


def test_a_failed_gate_says_the_alias_was_not_moved() -> None:
    with pytest.raises(EvaluationThresholdError, match="Champion alias is unchanged"):
        enforce_threshold(EvaluationResult(rmse=1e6, r2=-1.0))


def test_perfect_predictions_score_perfectly() -> None:
    y_true = np.array([1.0, 2.0, 3.0, 4.0])

    result = evaluate_predictions(y_true, y_true.copy())

    assert result.rmse == pytest.approx(0.0)
    assert result.r2 == pytest.approx(1.0)
    assert result.passed


def test_metrics_payload_carries_both_gate_metrics() -> None:
    assert EvaluationResult(rmse=1.5, r2=0.99).as_metrics() == {"rmse": 1.5, "r2": 0.99}


def test_evaluation_rejects_an_empty_prediction_set() -> None:
    with pytest.raises(ValueError, match="empty prediction set"):
        evaluate_predictions(np.array([]), np.array([]))


def test_evaluation_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        evaluate_predictions(np.array([1.0, 2.0]), np.array([1.0]))


# --- Environment / config validation ----------------------------------------


@pytest.mark.parametrize("catalog", ["dev", "stg", "prod"])
def test_each_governed_catalog_resolves(catalog: str) -> None:
    config = resolve_config({"CATALOG": catalog, "SCHEMA": PRODUCT_SCHEMA})

    assert config == ProductConfig(catalog=catalog, schema=PRODUCT_SCHEMA)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CATALOG": "dev"},
        {"SCHEMA": PRODUCT_SCHEMA},
        {"CATALOG": "", "SCHEMA": PRODUCT_SCHEMA},
        {"CATALOG": "dev", "SCHEMA": "   "},
    ],
)
def test_missing_environment_values_fail_closed(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="must be supplied by the job cluster environment"):
        resolve_config(env)


@pytest.mark.parametrize("catalog", ["sandbox", "main", "hive_metastore", "Dev"])
def test_an_ungoverned_catalog_is_refused(catalog: str) -> None:
    with pytest.raises(ValueError, match="CATALOG must be one of"):
        resolve_config({"CATALOG": catalog, "SCHEMA": PRODUCT_SCHEMA})


def test_another_products_schema_is_refused() -> None:
    with pytest.raises(ValueError, match=f"SCHEMA must be '{PRODUCT_SCHEMA}'"):
        resolve_config({"CATALOG": "dev", "SCHEMA": "hello_databricks"})


def test_surrounding_whitespace_is_tolerated() -> None:
    assert resolve_config({"CATALOG": " dev ", "SCHEMA": f" {PRODUCT_SCHEMA} "}) == ProductConfig(
        catalog="dev", schema=PRODUCT_SCHEMA
    )


def test_resolve_config_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in DEV_ENV.items():
        monkeypatch.setenv(key, value)

    assert resolve_config().catalog == "dev"


# --- Named feature contract -------------------------------------------------
#
# These guard the model's INPUT SIGNATURE, which is the seam that broke: a model
# logged from a bare NumPy array gets an anonymous Tensor('float64', (-1, 3))
# signature, and Spark inference can then only feed it positionally. Fitting and
# signing through a named-column DataFrame instead produces named scalar columns,
# which batch_inference satisfies with a struct of the same names.
#
# No Spark and no Databricks: mlflow.models.infer_signature is a pure local
# function, and LinearRegression fits 500 rows in milliseconds.


def _training_frame() -> pd.DataFrame:
    """The named feature/label frame, built exactly as train_register builds it."""
    return pd.DataFrame(training_records())


def _named_features(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[list(FEATURE_COLUMNS)].astype("float64")


def test_named_features_keep_canonical_column_order() -> None:
    features = _named_features(_training_frame())

    assert list(features.columns) == list(FEATURE_COLUMNS)
    assert len(features) == TRAINING_ROWS
    assert all(dtype == np.float64 for dtype in features.dtypes)


def test_signature_from_a_named_frame_has_named_scalar_columns() -> None:
    frame = _training_frame()
    features = _named_features(frame)
    labels = frame[LABEL_COLUMN].to_numpy(dtype=np.float64)

    model = LinearRegression().fit(features, labels)
    signature = infer_signature(features, model.predict(features))

    assert not signature.inputs.is_tensor_spec()
    assert signature.inputs.input_names() == list(FEATURE_COLUMNS)


def test_signature_from_a_bare_array_is_an_anonymous_tensor() -> None:
    """The regression this contract exists to prevent.

    Kept as an executable statement of the failure mode: the same data, handed
    over without column names, produces a signature Spark inference cannot match
    by name.
    """
    frame = _training_frame()
    features = _named_features(frame).to_numpy(dtype=np.float64)
    labels = frame[LABEL_COLUMN].to_numpy(dtype=np.float64)

    model = LinearRegression().fit(features, labels)
    signature = infer_signature(features, model.predict(features))

    assert signature.inputs.is_tensor_spec()


def test_a_named_model_scores_the_struct_payload_spark_will_send(
    tmp_path: Path,
) -> None:
    """End-to-end proof of the contract, through pyfunc and no further.

    mlflow.pyfunc.spark_udf hands a struct column over as a pandas DataFrame
    keyed by the struct's field names, then enforces the signature before
    calling the model. Saving and loading the model locally exercises that exact
    enforcement path with no tracking server, no Spark and no Databricks.
    """
    frame = _training_frame()
    features = _named_features(frame)
    labels = frame[LABEL_COLUMN].to_numpy(dtype=np.float64)

    model = LinearRegression().fit(features, labels)
    signature = infer_signature(features, model.predict(features))

    path = str(tmp_path / "model")
    mlflow.sklearn.save_model(sk_model=model, path=path, signature=signature)

    loaded = mlflow.pyfunc.load_model(path)

    struct_payload = features.head(5)
    np.testing.assert_allclose(loaded.predict(struct_payload), model.predict(struct_payload))


def test_pyfunc_scores_reordered_named_columns_by_name(
    tmp_path: Path,
) -> None:
    """Names, not positions, are the contract once the signature is named.

    Worth stating precisely, because the two layers behave differently: raw
    scikit-learn REJECTS a reordered frame outright (see the test below), while
    MLflow's signature enforcement reorders the columns to the signature before
    the model ever sees them. That reordering is what makes a struct of named
    fields safe to send, and it is what an anonymous tensor signature cannot do.
    """
    frame = _training_frame()
    features = _named_features(frame)
    labels = frame[LABEL_COLUMN].to_numpy(dtype=np.float64)

    model = LinearRegression().fit(features, labels)
    signature = infer_signature(features, model.predict(features))

    path = str(tmp_path / "model")
    mlflow.sklearn.save_model(sk_model=model, path=path, signature=signature)

    loaded = mlflow.pyfunc.load_model(path)

    canonical = features.head(5)
    reordered = canonical[list(reversed(FEATURE_COLUMNS))]

    np.testing.assert_allclose(loaded.predict(reordered), loaded.predict(canonical))


def test_raw_sklearn_rejects_reordered_named_columns() -> None:
    """Why the signature layer matters: the estimator itself will not reorder."""
    frame = _training_frame()
    features = _named_features(frame)
    labels = frame[LABEL_COLUMN].to_numpy(dtype=np.float64)

    model = LinearRegression().fit(features, labels)

    with pytest.raises(ValueError, match="feature names should match"):
        model.predict(features[list(reversed(FEATURE_COLUMNS))])
