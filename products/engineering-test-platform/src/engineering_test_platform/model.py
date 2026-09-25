"""Deterministic engineering surrogate model.

The model stands in for a real quality-prediction surrogate. It is intentionally
pure Python with no numerical dependencies: the point of this product is to
practise containerisation, health checking, release and rollback, so the model
must be fast, deterministic and trivial to reason about.
"""

import os

from pydantic import BaseModel, Field

DEFAULT_MODEL_VERSION = "engineering-surrogate-v1"

# Operating point at which the process is considered ideal. Deviation from any
# of these reduces predicted quality.
OPTIMUM_TEMPERATURE_C = 80.0
OPTIMUM_PRESSURE_BAR = 5.0
OPTIMUM_SPEED_RPM = 3000.0

# Quality lost per unit of deviation from the optimum.
TEMPERATURE_PENALTY_PER_C = 0.35
PRESSURE_PENALTY_PER_BAR = 2.0
SPEED_PENALTY_PER_RPM = 0.004

# predicted_quality is a percentage and is always clamped to this range.
MIN_QUALITY = 0.0
MAX_QUALITY = 100.0


class PredictionInput(BaseModel):
    """Process conditions for a single prediction."""

    temperature_c: float = Field(
        description="Process temperature in degrees Celsius.",
        ge=-50.0,
        le=500.0,
    )
    pressure_bar: float = Field(
        description="Process pressure in bar absolute.",
        ge=0.0,
        le=300.0,
    )
    rotational_speed_rpm: float = Field(
        description="Shaft rotational speed in revolutions per minute.",
        ge=0.0,
        le=20000.0,
    )


class PredictionOutput(BaseModel):
    """Predicted quality for a single set of process conditions."""

    predicted_quality: float = Field(
        description=f"Predicted quality score, clamped to [{MIN_QUALITY}, {MAX_QUALITY}].",
        ge=MIN_QUALITY,
        le=MAX_QUALITY,
    )
    model_version: str = Field(description="Version of the model that produced the prediction.")


class EngineeringSurrogate:
    """Deterministic quality surrogate.

    The same input always produces the same output: there is no randomness, no
    hidden state and no I/O in `predict`.
    """

    def __init__(self, model_version: str | None = None) -> None:
        self.model_version = model_version or os.environ.get("MODEL_VERSION", DEFAULT_MODEL_VERSION)

    def predict(self, features: PredictionInput) -> PredictionOutput:
        """Score one set of process conditions."""
        penalty = (
            abs(features.temperature_c - OPTIMUM_TEMPERATURE_C) * TEMPERATURE_PENALTY_PER_C
            + abs(features.pressure_bar - OPTIMUM_PRESSURE_BAR) * PRESSURE_PENALTY_PER_BAR
            + abs(features.rotational_speed_rpm - OPTIMUM_SPEED_RPM) * SPEED_PENALTY_PER_RPM
        )

        quality = clamp_quality(MAX_QUALITY - penalty)

        return PredictionOutput(
            predicted_quality=quality,
            model_version=self.model_version,
        )


def clamp_quality(value: float) -> float:
    """Clamp a raw score into the documented [0.0, 100.0] quality range."""
    clamped = min(max(value, MIN_QUALITY), MAX_QUALITY)

    # Round to keep the response stable across platforms; the model is meant to
    # be reproducible, and float noise in the last digits would undermine that.
    return round(clamped, 6)
