"""Unit tests for the Candidate -> Champion promotion contract.

Aliases, exact-version URIs, model-version tag construction and provenance
verification. All pure logic: no Spark, no Databricks, no MLflow client, no
credential.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ml_logic
from ml_logic import (
    BUNDLE_TARGET_TAG,
    CANDIDATE_ALIAS,
    CHAMPION_ALIAS,
    ENVIRONMENT_TAG,
    EVALUATION_R2_TAG,
    EVALUATION_RMSE_TAG,
    GIT_SHA_TAG,
    PRODUCT_NAME,
    PRODUCT_SCHEMA,
    PRODUCT_TAG,
    REGISTERED_AS_CANDIDATE,
    REGISTERED_AS_TAG,
    TRAINING_RUN_ID_TAG,
    EvaluationResult,
    ProductConfig,
    ProvenanceError,
    build_model_version_tags,
    build_run_tags,
    parse_model_version,
    verify_model_version_provenance,
)

SHA_A = "1111111111111111111111111111111111111111"
SHA_B = "2222222222222222222222222222222222222222"

PROD = ProductConfig(catalog="prod", schema=PRODUCT_SCHEMA)
RESULT = EvaluationResult(rmse=10.5, r2=0.97)

PIPELINE_ENV = {"GIT_SHA": SHA_A, "BUNDLE_TARGET": "prod"}


# --- Aliases ----------------------------------------------------------------


def test_the_two_aliases_are_distinct_and_stable() -> None:
    """Both names are part of the registry contract; neither may drift silently."""
    assert CANDIDATE_ALIAS == "Candidate"
    assert CHAMPION_ALIAS == "Champion"
    assert CANDIDATE_ALIAS != CHAMPION_ALIAS


def test_alias_uris_address_the_two_aliases() -> None:
    assert PROD.candidate_model_uri == (
        f"models:/prod.{PRODUCT_SCHEMA}.linear_regression_model@Candidate"
    )
    assert PROD.champion_model_uri == (
        f"models:/prod.{PRODUCT_SCHEMA}.linear_regression_model@Champion"
    )


# --- Exact-version URIs -----------------------------------------------------


def test_version_uri_pins_an_exact_number_not_an_alias() -> None:
    uri = PROD.version_model_uri(7)

    assert uri == f"models:/prod.{PRODUCT_SCHEMA}.linear_regression_model/7"
    assert "@" not in uri


def test_version_uri_accepts_the_string_a_cli_returns() -> None:
    assert PROD.version_model_uri("7") == PROD.version_model_uri(7)


@pytest.mark.parametrize("version", ["", "   ", "latest", "Candidate", "-1", "1.0", "7a"])
def test_version_uri_fails_closed_on_a_malformed_version(version: str) -> None:
    """An empty string or unresolved macro must never become part of a URI."""
    with pytest.raises(ProvenanceError):
        PROD.version_model_uri(version)


def test_version_uri_rejects_zero() -> None:
    with pytest.raises(ProvenanceError, match="positive integer"):
        PROD.version_model_uri(0)


def test_parse_model_version_reports_an_empty_version_distinctly() -> None:
    with pytest.raises(ProvenanceError, match="model version is empty"):
        parse_model_version("")


def test_parse_model_version_tolerates_surrounding_whitespace() -> None:
    assert parse_model_version(" 12 ") == 12


# --- Candidate predictions table --------------------------------------------


def test_candidate_output_is_a_separate_table_from_production_predictions() -> None:
    """A candidate must not be able to overwrite what production consumers read."""
    assert PROD.candidate_predictions_table == f"prod.{PRODUCT_SCHEMA}.candidate_predictions"
    assert PROD.predictions_table == f"prod.{PRODUCT_SCHEMA}.predictions"
    assert PROD.candidate_predictions_table != PROD.predictions_table


# --- Run tags ---------------------------------------------------------------


def test_run_tags_carry_environment_and_product() -> None:
    tags = build_run_tags(PROD, {})

    assert tags[ENVIRONMENT_TAG] == "prod"
    assert tags[PRODUCT_TAG] == PRODUCT_NAME


def test_run_tags_record_pipeline_provenance_when_supplied() -> None:
    tags = build_run_tags(PROD, PIPELINE_ENV)

    assert tags[GIT_SHA_TAG] == SHA_A
    assert tags[BUNDLE_TARGET_TAG] == "prod"


def test_run_tags_omit_provenance_that_was_not_supplied() -> None:
    """An interactive run outside a Git checkout must still be possible."""
    tags = build_run_tags(PROD, {"GIT_SHA": "", "BUNDLE_TARGET": "   "})

    assert GIT_SHA_TAG not in tags
    assert BUNDLE_TARGET_TAG not in tags


# --- Model-version tags -----------------------------------------------------


def test_model_version_tags_include_every_required_key() -> None:
    tags = build_model_version_tags(PROD, "run-abc", RESULT, PIPELINE_ENV)

    assert set(tags) == {
        PRODUCT_TAG,
        ENVIRONMENT_TAG,
        GIT_SHA_TAG,
        BUNDLE_TARGET_TAG,
        TRAINING_RUN_ID_TAG,
        EVALUATION_RMSE_TAG,
        EVALUATION_R2_TAG,
        REGISTERED_AS_TAG,
    }


def test_every_version_is_registered_as_a_candidate() -> None:
    """Training never produces a champion, in any environment."""
    for catalog in ("dev", "stg", "prod"):
        config = ProductConfig(catalog=catalog, schema=PRODUCT_SCHEMA)
        tags = build_model_version_tags(config, "run-abc", RESULT, PIPELINE_ENV)

        assert tags[REGISTERED_AS_TAG] == REGISTERED_AS_CANDIDATE


def test_registered_as_is_immutable_provenance_not_a_release_status() -> None:
    """The tag records how a version was CREATED; the alias records what is current.

    Guards the semantic split directly: there is no 'champion' tag value to
    write, so nothing can rewrite this tag later and leave the registry with two
    competing answers to "what is Champion?", one of which could go stale.
    """
    assert REGISTERED_AS_TAG == "registered_as"
    assert REGISTERED_AS_CANDIDATE == "candidate"

    exported = {name for name in dir(ml_logic) if "CHAMPION" in name}
    assert exported == {"CHAMPION_ALIAS"}, (
        f"Champion must exist only as an alias, never as a tag value: {exported}"
    )


def test_model_version_tags_carry_the_evaluation_evidence() -> None:
    """The approver's evidence and the approved artefact must be one object."""
    tags = build_model_version_tags(PROD, "run-abc", RESULT, PIPELINE_ENV)

    assert tags[EVALUATION_RMSE_TAG] == "10.500000"
    assert tags[EVALUATION_R2_TAG] == "0.970000"
    assert tags[TRAINING_RUN_ID_TAG] == "run-abc"


def test_model_version_tags_are_all_strings() -> None:
    """Unity Catalog tag values are strings; a float would not round-trip."""
    tags = build_model_version_tags(PROD, "run-abc", RESULT, PIPELINE_ENV)

    assert all(isinstance(value, str) for value in tags.values())


def test_model_version_tags_are_a_superset_of_the_run_tags() -> None:
    run_tags = build_run_tags(PROD, PIPELINE_ENV)
    version_tags = build_model_version_tags(PROD, "run-abc", RESULT, PIPELINE_ENV)

    assert run_tags.items() <= version_tags.items()


def test_model_version_tags_omit_absent_provenance() -> None:
    tags = build_model_version_tags(PROD, "run-abc", RESULT, {})

    assert GIT_SHA_TAG not in tags
    assert BUNDLE_TARGET_TAG not in tags
    assert tags[REGISTERED_AS_TAG] == REGISTERED_AS_CANDIDATE


@pytest.mark.parametrize("run_id", ["", "   "])
def test_model_version_tags_require_a_training_run_id(run_id: str) -> None:
    with pytest.raises(ValueError, match="training_run_id must not be empty"):
        build_model_version_tags(PROD, run_id, RESULT, PIPELINE_ENV)


# --- Provenance verification ------------------------------------------------


def test_provenance_accepts_a_matching_version() -> None:
    tags = build_model_version_tags(PROD, "run-abc", RESULT, PIPELINE_ENV)

    verify_model_version_provenance(tags, expected_environment="prod", expected_git_sha=SHA_A)


def test_provenance_rejects_a_model_trained_in_another_environment() -> None:
    """The serious mistake: promoting an artefact fitted to another catalog."""
    tags = build_model_version_tags(
        ProductConfig(catalog="stg", schema=PRODUCT_SCHEMA), "run-abc", RESULT, PIPELINE_ENV
    )

    with pytest.raises(ProvenanceError, match="trained in 'stg', expected 'prod'"):
        verify_model_version_provenance(tags, expected_environment="prod")


def test_provenance_rejects_a_model_built_from_another_commit() -> None:
    tags = build_model_version_tags(PROD, "run-abc", RESULT, PIPELINE_ENV)

    with pytest.raises(ProvenanceError, match=f"trained from commit {SHA_A}"):
        verify_model_version_provenance(tags, expected_environment="prod", expected_git_sha=SHA_B)


def test_provenance_fails_closed_when_a_demanded_git_sha_is_missing() -> None:
    """Absent provenance must not read as satisfied provenance."""
    tags = build_model_version_tags(PROD, "run-abc", RESULT, {})

    with pytest.raises(ProvenanceError, match="carries no 'git_sha' tag"):
        verify_model_version_provenance(tags, expected_environment="prod", expected_git_sha=SHA_A)


def test_provenance_fails_closed_when_the_environment_tag_is_missing() -> None:
    with pytest.raises(ProvenanceError, match="carries no 'environment' tag"):
        verify_model_version_provenance({}, expected_environment="prod")


@pytest.mark.parametrize("git_sha", [None, "", "   "])
def test_provenance_skips_the_commit_check_when_none_was_supplied(git_sha: str | None) -> None:
    """Outside a Git checkout there is no commit to compare against."""
    tags = build_model_version_tags(PROD, "run-abc", RESULT, {})

    verify_model_version_provenance(tags, expected_environment="prod", expected_git_sha=git_sha)


def test_provenance_verifies_exactly_what_training_wrote() -> None:
    """End-to-end on the pure layer: build the tags, then verify them."""
    for catalog in ("dev", "stg", "prod"):
        config = ProductConfig(catalog=catalog, schema=PRODUCT_SCHEMA)
        tags = build_model_version_tags(config, "run-abc", RESULT, PIPELINE_ENV)

        verify_model_version_provenance(tags, expected_environment=catalog, expected_git_sha=SHA_A)


# --- Cross-language consistency ---------------------------------------------


def test_the_promotion_helper_agrees_with_python_on_the_product_name() -> None:
    """The manifest's `product` field is written by Bash and named in Python.

    They are separate literals in separate languages, and nothing else would
    catch a rename in one that was not made in the other — the symptom would be
    a PROD release stage rejecting every manifest it was handed.
    """
    helper = Path(__file__).resolve().parents[2] / "scripts" / "model-promotion.sh"

    assert helper.is_file()
    assert f'readonly PRODUCT_NAME="{PRODUCT_NAME}"' in helper.read_text()
