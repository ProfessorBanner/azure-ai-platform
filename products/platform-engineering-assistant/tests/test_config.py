"""Versioned retrieval configuration: loading and range enforcement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from platform_engineering_assistant.config import (
    DEFAULT_RETRIEVAL_CONFIG_PATH,
    MAX_TOP_K,
    load_retrieval_config,
)
from platform_engineering_assistant.errors import ConfigurationError, FailureCategory

VALID: dict[str, Any] = {
    "version": "retrieval_v1",
    "top_k": 6,
    "minimum_score": 0.5,
    "bm25_k1": 1.2,
    "bm25_b": 0.75,
    "chunk_budget_chars": 1200,
    "title_weight": 0.0,
    "heading_weight": 0.25,
}


def write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "retrieval.json"
    path.write_text(json.dumps(payload))
    return path


# --- the shipped file -------------------------------------------------------


def test_shipped_configuration_loads() -> None:
    config = load_retrieval_config()
    assert config.version == "retrieval_v1"
    assert config.top_k >= 1
    assert config.chunk_budget_chars > 0


def test_shipped_configuration_declares_a_minimum_score() -> None:
    """The refusal path depends on it, so its absence must not be tolerable."""
    raw = json.loads(DEFAULT_RETRIEVAL_CONFIG_PATH.read_text())
    assert "minimum_score" in raw
    assert load_retrieval_config().minimum_score >= 0.0


def test_configuration_is_loaded_from_a_file_not_hard_coded(tmp_path: Path) -> None:
    """A different file must produce different values."""
    path = write(tmp_path, {**VALID, "top_k": 9, "minimum_score": 2.5})
    config = load_retrieval_config(path)
    assert config.top_k == 9
    assert config.minimum_score == 2.5


# --- structural failures ----------------------------------------------------


def test_missing_file_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_retrieval_config(tmp_path / "absent.json")
    assert caught.value.category is FailureCategory.CONFIGURATION


def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "retrieval.json"
    path.write_text("{not json")
    with pytest.raises(ConfigurationError):
        load_retrieval_config(path)


def test_non_object_payload_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_retrieval_config(write(tmp_path, [1, 2, 3]))


@pytest.mark.parametrize("field", sorted(VALID))
def test_every_field_is_required(tmp_path: Path, field: str) -> None:
    payload = {key: value for key, value in VALID.items() if key != field}
    with pytest.raises(ConfigurationError) as caught:
        load_retrieval_config(write(tmp_path, payload))
    assert field in str(caught.value)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    """A typo must be an error, not an ignored key leaving a stale setting."""
    with pytest.raises(ConfigurationError) as caught:
        load_retrieval_config(write(tmp_path, {**VALID, "top_kk": 6}))
    assert "top_kk" in str(caught.value)


def test_documentation_keys_are_ignored(tmp_path: Path) -> None:
    config = load_retrieval_config(write(tmp_path, {**VALID, "$comment": ["notes"]}))
    assert config.top_k == 6


# --- value ranges -----------------------------------------------------------


@pytest.mark.parametrize("version", ["v1", "retrieval_v0", "retrieval_va", "", "RETRIEVAL_V1"])
def test_invalid_version_is_rejected(tmp_path: Path, version: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_retrieval_config(write(tmp_path, {**VALID, "version": version}))
    assert "version" in str(caught.value)


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1, 1000])
def test_top_k_outside_the_bounded_range_is_rejected(tmp_path: Path, top_k: int) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_retrieval_config(write(tmp_path, {**VALID, "top_k": top_k}))
    assert "top_k" in str(caught.value)


@pytest.mark.parametrize("minimum_score", [-0.1, -1, -1000.0])
def test_negative_minimum_score_is_rejected(tmp_path: Path, minimum_score: float) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_retrieval_config(write(tmp_path, {**VALID, "minimum_score": minimum_score}))
    assert "minimum_score" in str(caught.value)


def test_zero_minimum_score_is_permitted(tmp_path: Path) -> None:
    """Zero is a deliberate 'admit anything retrieval returns', not an error."""
    assert (
        load_retrieval_config(write(tmp_path, {**VALID, "minimum_score": 0})).minimum_score == 0.0
    )


@pytest.mark.parametrize("k1", [0, -1.0])
def test_non_positive_bm25_k1_is_rejected(tmp_path: Path, k1: float) -> None:
    with pytest.raises(ConfigurationError):
        load_retrieval_config(write(tmp_path, {**VALID, "bm25_k1": k1}))


@pytest.mark.parametrize("b", [-0.01, 1.01, 5.0])
def test_bm25_b_outside_zero_to_one_is_rejected(tmp_path: Path, b: float) -> None:
    with pytest.raises(ConfigurationError):
        load_retrieval_config(write(tmp_path, {**VALID, "bm25_b": b}))


@pytest.mark.parametrize("budget", [0, 10, 199, 8001, 1_000_000])
def test_unreasonable_chunk_budget_is_rejected(tmp_path: Path, budget: int) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_retrieval_config(write(tmp_path, {**VALID, "chunk_budget_chars": budget}))
    assert "chunk_budget_chars" in str(caught.value)


def test_boolean_is_not_accepted_as_a_number(tmp_path: Path) -> None:
    """bool subclasses int; `true` must not silently become 1."""
    with pytest.raises(ConfigurationError):
        load_retrieval_config(write(tmp_path, {**VALID, "top_k": True}))


def test_non_integer_top_k_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_retrieval_config(write(tmp_path, {**VALID, "top_k": 6.5}))


# --- metadata weights (17.1b) ------------------------------------------------


def test_shipped_configuration_declares_metadata_weights() -> None:
    config = load_retrieval_config()
    assert config.title_weight >= 0.0
    assert config.heading_weight >= 0.0


@pytest.mark.parametrize("field", ["title_weight", "heading_weight"])
def test_negative_metadata_weight_is_rejected(tmp_path: Path, field: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_retrieval_config(write(tmp_path, {**VALID, field: -0.5}))
    assert field in str(caught.value)


@pytest.mark.parametrize("field", ["title_weight", "heading_weight"])
def test_metadata_weights_are_required(tmp_path: Path, field: str) -> None:
    payload = {key: value for key, value in VALID.items() if key != field}
    with pytest.raises(ConfigurationError):
        load_retrieval_config(write(tmp_path, payload))


def test_zero_metadata_weights_are_permitted(tmp_path: Path) -> None:
    """Zero is the deliberate 'pure body BM25' setting, not an error."""
    config = load_retrieval_config(
        write(tmp_path, {**VALID, "title_weight": 0.0, "heading_weight": 0.0})
    )
    assert config.title_weight == 0.0
    assert config.heading_weight == 0.0


def test_minimum_score_is_documented_as_a_no_signal_floor() -> None:
    """It cannot separate in-scope from out-of-scope; 17.1c owns refusal."""
    raw = DEFAULT_RETRIEVAL_CONFIG_PATH.read_text().lower()
    assert "no-signal floor" in raw
    assert "17.1c" in raw
