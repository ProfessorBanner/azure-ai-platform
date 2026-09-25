"""Unit tests for controlled retraining: the newly-labelled training set.

Retraining exists to learn from observations the current Champion never saw. A
retrain that quietly refitted the unchanged training_data would look identical
in every log and metric, so these tests pin the difference down: the join, its
failure modes, deterministic deduplication, which table training reads, and the
job wiring that connects them.

Pure logic and a YAML parse. No Spark, no Databricks, no credential.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ml_logic import (
    ALLOWED_TRAINING_TABLES,
    BATCH_ROWS,
    FEATURE_COLUMNS,
    ID_COLUMN,
    LABEL_COLUMN,
    PRODUCT_SCHEMA,
    TRAINING_ROWS,
    ProductConfig,
    RetrainingDataError,
    actuals_records,
    batch_input_records,
    combine_training_sets,
    join_labelled_observations,
    select_training_table,
    training_records,
)

PROD = ProductConfig(catalog="prod", schema=PRODUCT_SCHEMA)

JOB_YAML = Path(__file__).resolve().parents[2] / "resources" / "job.yml"


def feature_row(row_id: float, offset: float = 0.0) -> dict[str, float]:
    return {
        ID_COLUMN: row_id,
        **{name: offset + index for index, name in enumerate(FEATURE_COLUMNS)},
    }


def actual_row(row_id: float, label: float = 1.0) -> dict[str, float]:
    return {ID_COLUMN: row_id, LABEL_COLUMN: label}


# --- Joining features to delayed labels -------------------------------------


def test_join_pairs_each_batch_row_with_its_label() -> None:
    joined = join_labelled_observations(
        [feature_row(1.0), feature_row(2.0)],
        [actual_row(2.0, 20.0), actual_row(1.0, 10.0)],
    )

    assert [row[ID_COLUMN] for row in joined] == [1.0, 2.0]
    assert [row[LABEL_COLUMN] for row in joined] == [10.0, 20.0]


def test_join_produces_rows_shaped_like_training_data() -> None:
    """The result must be interchangeable with training_data for train_register."""
    joined = join_labelled_observations([feature_row(1.0)], [actual_row(1.0)])

    assert set(joined[0]) == {ID_COLUMN, *FEATURE_COLUMNS, LABEL_COLUMN}
    assert set(joined[0]) == set(training_records()[0])


def test_join_matches_by_row_id_not_by_position() -> None:
    """Actuals arrive later and in no guaranteed order."""
    joined = join_labelled_observations(
        [feature_row(7.0), feature_row(8.0)],
        [actual_row(8.0, 80.0), actual_row(7.0, 70.0)],
    )

    assert joined[0][ID_COLUMN] == 7.0
    assert joined[0][LABEL_COLUMN] == 70.0


def test_the_products_own_batch_and_actuals_join_cleanly() -> None:
    """End-to-end on the real generated data, not just fixtures."""
    joined = join_labelled_observations(batch_input_records(), actuals_records())

    assert len(joined) == BATCH_ROWS
    assert joined[0][ID_COLUMN] == float(TRAINING_ROWS)


# --- Join failure modes -----------------------------------------------------


def test_a_batch_row_without_a_label_fails_closed() -> None:
    """Training on the labelled subset alone is a biased sample nobody chose."""
    with pytest.raises(RetrainingDataError, match="have no label, starting at row_id 2"):
        join_labelled_observations([feature_row(1.0), feature_row(2.0)], [actual_row(1.0)])


def test_the_missing_label_message_counts_them() -> None:
    with pytest.raises(RetrainingDataError, match="2 batch row"):
        join_labelled_observations(
            [feature_row(1.0), feature_row(2.0), feature_row(3.0)], [actual_row(1.0)]
        )


def test_duplicate_feature_ids_fail_closed() -> None:
    """A duplicated row_id would silently weight one observation double."""
    with pytest.raises(RetrainingDataError, match="duplicate row_id values in batch feature"):
        join_labelled_observations([feature_row(1.0), feature_row(1.0)], [actual_row(1.0)])


def test_duplicate_actual_ids_fail_closed() -> None:
    with pytest.raises(RetrainingDataError, match="duplicate row_id values in actuals"):
        join_labelled_observations([feature_row(1.0)], [actual_row(1.0, 1.0), actual_row(1.0, 2.0)])


def test_no_ground_truth_at_all_fails_closed() -> None:
    """Nothing new to learn — a retrain here would just refit the original data."""
    with pytest.raises(RetrainingDataError, match="have no label"):
        join_labelled_observations([feature_row(1.0)], [])


def test_an_empty_batch_fails_closed() -> None:
    with pytest.raises(RetrainingDataError, match="no batch features supplied"):
        join_labelled_observations([], [actual_row(1.0)])


# --- Deterministic deduplication --------------------------------------------


def test_newly_labelled_observations_win_on_collision() -> None:
    """Later ground truth is the more recent statement about a row."""
    combined = combine_training_sets(
        [{**feature_row(1.0), LABEL_COLUMN: 100.0}],
        [{**feature_row(1.0), LABEL_COLUMN: 999.0}],
    )

    assert len(combined) == 1
    assert combined[0][LABEL_COLUMN] == 999.0


def test_combining_is_sorted_by_row_id() -> None:
    """An unstable row order would make the training seed meaningless."""
    combined = combine_training_sets(
        [{**feature_row(5.0), LABEL_COLUMN: 1.0}, {**feature_row(1.0), LABEL_COLUMN: 1.0}],
        [{**feature_row(3.0), LABEL_COLUMN: 1.0}],
    )

    assert [row[ID_COLUMN] for row in combined] == [1.0, 3.0, 5.0]


def test_combining_is_deterministic_across_calls() -> None:
    original = training_records()
    newly = join_labelled_observations(batch_input_records(), actuals_records())

    assert combine_training_sets(original, newly) == combine_training_sets(original, newly)


def test_combining_is_insensitive_to_input_order() -> None:
    original = training_records()
    newly = join_labelled_observations(batch_input_records(), actuals_records())

    assert combine_training_sets(original, newly) == combine_training_sets(
        list(reversed(original)), list(reversed(newly))
    )


def test_the_retraining_set_is_larger_than_the_original() -> None:
    """The whole point: the candidate sees data the Champion never did."""
    original = training_records()
    newly = join_labelled_observations(batch_input_records(), actuals_records())

    combined = combine_training_sets(original, newly)

    assert len(combined) == TRAINING_ROWS + BATCH_ROWS
    assert len(combined) > len(original)


def test_combining_does_not_mutate_its_inputs() -> None:
    original = [{**feature_row(1.0), LABEL_COLUMN: 1.0}]
    newly = [{**feature_row(1.0), LABEL_COLUMN: 2.0}]

    combine_training_sets(original, newly)

    assert original[0][LABEL_COLUMN] == 1.0
    assert newly[0][LABEL_COLUMN] == 2.0


def test_combining_with_nothing_new_returns_the_original_set() -> None:
    original = training_records()

    assert combine_training_sets(original, []) == sorted(original, key=lambda row: row[ID_COLUMN])


# --- Training-table selection -----------------------------------------------


def test_the_default_is_the_governed_training_table() -> None:
    """The ordinary candidate job passes nothing and must be unaffected."""
    assert select_training_table(PROD) == PROD.training_table
    assert select_training_table(PROD, None) == f"prod.{PRODUCT_SCHEMA}.training_data"


def test_retraining_selects_the_retraining_table() -> None:
    assert select_training_table(PROD, "retraining_data") == (
        f"prod.{PRODUCT_SCHEMA}.retraining_data"
    )


@pytest.mark.parametrize("catalog", ["dev", "stg", "prod"])
def test_selection_qualifies_with_the_running_environment(catalog: str) -> None:
    """The catalog never comes from the argument, so it cannot be redirected."""
    config = ProductConfig(catalog=catalog, schema=PRODUCT_SCHEMA)

    assert select_training_table(config, "retraining_data").startswith(f"{catalog}.")


@pytest.mark.parametrize(
    "requested",
    [
        "predictions",
        "inference_actuals",
        "dev.ml_lifecycle_demo.training_data",
        "prod.other_product.training_data",
        "../training_data",
        "",
        "   ",
    ],
)
def test_selection_refuses_anything_outside_the_allowlist(requested: str) -> None:
    """A job parameter that could name any table is a job parameter worth abusing."""
    with pytest.raises(ValueError):
        select_training_table(PROD, requested)


def test_a_qualified_name_is_rejected_even_for_an_allowed_table() -> None:
    """Short names only — that is what makes redirection structurally impossible."""
    with pytest.raises(ValueError, match="training table must be one of"):
        select_training_table(PROD, f"prod.{PRODUCT_SCHEMA}.training_data")


def test_the_allowlist_is_exactly_the_two_governed_training_tables() -> None:
    assert set(ALLOWED_TRAINING_TABLES) == {"training_data", "retraining_data"}


def test_selection_tolerates_surrounding_whitespace() -> None:
    assert select_training_table(PROD, " retraining_data ") == PROD.retraining_data_table


# --- Monitoring job wiring --------------------------------------------------
#
# The Python above is only correct if the job actually calls it that way. These
# parse the deployed Bundle definition rather than trusting that it matches.


def job(name: str) -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load(JOB_YAML.read_text())
    found: dict[str, Any] = parsed["resources"]["jobs"][name]

    return found


def monitor_job() -> dict[str, Any]:
    return job("ml_monitor_retrain")


def tasks_by_key() -> dict[str, dict[str, Any]]:
    return {task["task_key"]: task for task in monitor_job()["tasks"]}


def test_the_retraining_graph_is_wired_in_order() -> None:
    tasks = tasks_by_key()

    assert set(tasks) == {
        "monitor",
        "retraining_required",
        "prepare_retraining_data",
        "train_register",
        "validate_candidate",
    }

    def depends(key: str) -> list[dict[str, str]]:
        found: list[dict[str, str]] = tasks[key].get("depends_on", [])

        return found

    assert depends("monitor") == []
    assert depends("retraining_required") == [{"task_key": "monitor"}]
    assert depends("prepare_retraining_data") == [
        {"task_key": "retraining_required", "outcome": "true"}
    ]
    assert depends("train_register") == [{"task_key": "prepare_retraining_data"}]
    assert depends("validate_candidate") == [{"task_key": "train_register"}]


def test_retraining_only_runs_on_the_true_branch() -> None:
    """A healthy monitor run must not start a cluster to retrain nothing."""
    condition = tasks_by_key()["retraining_required"]["condition_task"]

    assert condition == {
        "op": "EQUAL_TO",
        "left": "{{tasks.monitor.values.retraining_required}}",
        "right": "true",
    }


def test_the_retraining_job_fits_on_the_retraining_table() -> None:
    """The correction this whole change exists for."""
    parameters = tasks_by_key()["train_register"]["spark_python_task"]["parameters"]

    assert "--training-table" in parameters
    assert parameters[parameters.index("--training-table") + 1] == "retraining_data"


def test_the_candidate_job_keeps_the_governed_default() -> None:
    """ml_candidate must be untouched by the retraining change."""
    candidate = job("ml_candidate")
    train = next(t for t in candidate["tasks"] if t["task_key"] == "train_register")

    assert "--training-table" not in train["spark_python_task"]["parameters"]
    assert [t["task_key"] for t in candidate["tasks"]] == [
        "prepare_data",
        "train_register",
        "validate_candidate",
    ]


def test_retraining_never_promotes() -> None:
    """No task in the monitoring job touches inference or an alias."""
    files = [
        task["spark_python_task"]["python_file"]
        for task in monitor_job()["tasks"]
        if "spark_python_task" in task
    ]

    assert not any("batch_inference" in path for path in files)
    assert all("promote" not in path for path in files)


def test_the_monitoring_job_serialises_its_runs() -> None:
    assert monitor_job()["max_concurrent_runs"] == 1
