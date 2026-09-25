import pytest

from engineering_test_platform.model import (
    DEFAULT_MODEL_VERSION,
    MAX_QUALITY,
    MIN_QUALITY,
    EngineeringSurrogate,
    PredictionInput,
    clamp_quality,
)


def test_prediction_is_deterministic() -> None:
    model = EngineeringSurrogate()
    features = PredictionInput(
        temperature_c=95.0,
        pressure_bar=6.5,
        rotational_speed_rpm=3200.0,
    )

    first = model.predict(features)
    second = model.predict(features)

    assert first.predicted_quality == second.predicted_quality
    assert first.model_version == second.model_version


def test_optimum_conditions_score_maximum_quality() -> None:
    model = EngineeringSurrogate()
    features = PredictionInput(
        temperature_c=80.0,
        pressure_bar=5.0,
        rotational_speed_rpm=3000.0,
    )

    assert model.predict(features).predicted_quality == MAX_QUALITY


def test_extreme_conditions_clamp_to_minimum_quality() -> None:
    model = EngineeringSurrogate()
    features = PredictionInput(
        temperature_c=500.0,
        pressure_bar=300.0,
        rotational_speed_rpm=20000.0,
    )

    # The raw penalty far exceeds the baseline; the output must not go negative.
    assert model.predict(features).predicted_quality == MIN_QUALITY


def test_clamp_quality_bounds_both_ends() -> None:
    assert clamp_quality(-25.0) == MIN_QUALITY
    assert clamp_quality(250.0) == MAX_QUALITY
    assert clamp_quality(42.5) == 42.5


def test_model_version_defaults_and_honours_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_VERSION", raising=False)
    assert EngineeringSurrogate().model_version == DEFAULT_MODEL_VERSION

    monkeypatch.setenv("MODEL_VERSION", "engineering-surrogate-v2")
    assert EngineeringSurrogate().model_version == "engineering-surrogate-v2"


def test_input_validation_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError):
        PredictionInput(
            temperature_c=10_000.0,
            pressure_bar=5.0,
            rotational_speed_rpm=3000.0,
        )
