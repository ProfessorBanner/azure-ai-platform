"""Version-controlled thresholds and the gate they define.

Thresholds live in JSON under `data/` rather than in code or in an environment
variable. Changing what counts as acceptable is a decision that should appear in
a diff and be reviewed; a runtime flag that quietly lowers a bar until the run
passes is exactly the failure mode this arrangement prevents.

A threshold may be `gated` (it decides the exit code) or not (it is measured and
reported, but never fails a run). Latency and severity are ungated on purpose:
both say more about a shared sandbox deployment and about human disagreement
than about whether this code works.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from foundry_capability_lab.errors import ConfigurationError
from foundry_capability_lab.evaluation.metrics import EvaluationMetrics

DEFAULT_THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "evaluation_thresholds.json"
)


class Threshold(BaseModel):
    """A single bound. Exactly one of `minimum` or `maximum` must be set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gated: bool
    minimum: float | None = None
    maximum: float | None = None
    rationale: str = ""

    def check(self, value: float) -> bool:
        """True when `value` satisfies the bound."""
        if self.minimum is not None and value < self.minimum:
            return False
        if self.maximum is not None and value > self.maximum:
            return False
        return True

    def describe(self) -> str:
        if self.minimum is not None:
            return f">= {self.minimum}"
        if self.maximum is not None:
            return f"<= {self.maximum}"
        return "unbounded"


class ThresholdSet(BaseModel):
    """The full, versioned threshold configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    thresholds: dict[str, Threshold]


class GateResult(BaseModel):
    """The outcome of checking one metric against its threshold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str
    value: float
    bound: str
    gated: bool
    passed: bool


def load_thresholds(path: Path | None = None) -> ThresholdSet:
    """Load and validate the threshold file.

    Raises:
        ConfigurationError: if the file is missing or malformed, or if any
            threshold sets neither bound (which would silently never fail).
    """
    thresholds_path = DEFAULT_THRESHOLDS_PATH if path is None else path

    try:
        raw = json.loads(thresholds_path.read_text())
    except OSError as exc:
        raise ConfigurationError(f"Threshold file could not be read: {thresholds_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{thresholds_path.name} is not valid JSON: {exc.msg}") from exc

    # Keys beginning with `$` are documentation for humans reading the file.
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if not key.startswith("$")}

    try:
        thresholds = ThresholdSet.model_validate(raw)
    except Exception as exc:
        raise ConfigurationError(f"{thresholds_path.name} is not a valid threshold set.") from exc

    unbounded = sorted(
        name
        for name, threshold in thresholds.thresholds.items()
        if threshold.minimum is None and threshold.maximum is None
    )
    if unbounded:
        raise ConfigurationError(
            f"Thresholds without a minimum or maximum can never fail: {', '.join(unbounded)}"
        )

    return thresholds


def evaluate_gates(metrics: EvaluationMetrics, thresholds: ThresholdSet) -> list[GateResult]:
    """Check every configured threshold against the measured metrics.

    Raises:
        ConfigurationError: if a threshold names a metric that does not exist.
            A typo must not silently become an unenforced gate.
    """
    measured = metrics.model_dump()
    results: list[GateResult] = []

    for name, threshold in sorted(thresholds.thresholds.items()):
        if name not in measured:
            raise ConfigurationError(
                f"Threshold '{name}' does not correspond to any computed metric."
            )
        value = float(measured[name])
        results.append(
            GateResult(
                metric=name,
                value=value,
                bound=threshold.describe(),
                gated=threshold.gated,
                passed=threshold.check(value),
            )
        )

    return results


def gates_passed(results: list[GateResult]) -> bool:
    """True when every GATED threshold passed. Ungated results never fail a run."""
    return all(result.passed for result in results if result.gated)
