from __future__ import annotations

import pytest

from pet_classifier.config import TrainingConfig, config_for_profile


def test_smoke_profile_matches_the_brief() -> None:
    config = config_for_profile("smoke")
    assert config.is_smoke
    assert (config.epochs, config.batch_size, config.num_workers) == (1, 8, 0)
    assert (config.train_per_class, config.val_per_class) == (4, 2)
    assert config.device == "cpu"


def test_full_profile_uses_every_sample() -> None:
    config = config_for_profile("full")
    assert not config.is_smoke
    assert config.train_per_class is None and config.val_per_class is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"epochs": 0},
        {"batch_size": 0},
        {"learning_rate": 0.0},
        {"learning_rate": 1.5},
        {"num_workers": -1},
        {"device": "tpu"},
        {"train_per_class": 0},
    ],
)
def test_invalid_values_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        config_for_profile("smoke", **overrides)


def test_smoke_profile_must_cap_samples() -> None:
    with pytest.raises(ValueError):
        TrainingConfig(profile="smoke", train_per_class=None)


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError):
        config_for_profile("production")


def test_config_is_frozen() -> None:
    config = config_for_profile("smoke")
    with pytest.raises(AttributeError):
        config.epochs = 2  # type: ignore[misc]


def test_params_are_strings() -> None:
    params = config_for_profile("smoke").as_params()
    assert all(isinstance(value, str) for value in params.values())
    assert params["profile"] == "smoke"
