"""The versioned, server-owned evaluation policy and the gate it defines.

Thresholds live in `evaluation/evaluation_policy_v1.json`, not in code and not
in the environment. Changing what counts as acceptable is a decision that must
appear in a diff; a runtime flag that quietly lowers a bar until a run passes is
exactly the failure mode this arrangement exists to prevent.

THREE OUTCOMES, NOT TWO
-----------------------
A gate reports PASS, FAIL or INCOMPLETE. The third is the important one: if a
required metric could not be computed — no judge ran, no tokens were reported,
every call failed — the honest verdict is "unknown", and collapsing that into
PASS is how a suite silently stops testing anything. A missing metric therefore
never satisfies a threshold.

FAIL DOMINATES INCOMPLETE. A definite breach is information; a missing metric is
the absence of it. A run with one failed gate and one unmeasured gate is a
failing run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from platform_engineering_assistant.config import PRODUCT_ROOT
from platform_engineering_assistant.errors import ConfigurationError

DEFAULT_POLICY_PATH = PRODUCT_ROOT / "evaluation" / "evaluation_policy_v1.json"


class ExecutionMode(StrEnum):
    """How an evaluation run obtained its answers."""

    FAKE = "fake"
    LIVE = "live"


class GateStatus(StrEnum):
    """The verdict of one gate, or of a whole run."""

    PASS = "pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"


class Threshold(BaseModel):
    """One bound, and the modes it applies to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modes: tuple[ExecutionMode, ...] = Field(min_length=1)
    requires_judge: bool
    minimum: float | None = None
    maximum: float | None = None
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def exactly_one_bound(self) -> Threshold:
        if (self.minimum is None) == (self.maximum is None):
            raise ValueError("a threshold must set exactly one of minimum or maximum")
        return self

    def applies_to(self, mode: ExecutionMode, *, judge_enabled: bool) -> bool:
        if mode not in self.modes:
            return False
        return judge_enabled if self.requires_judge else True

    def satisfied_by(self, value: float) -> bool:
        if self.minimum is not None:
            return value >= self.minimum
        assert self.maximum is not None
        return value <= self.maximum

    def describe(self) -> str:
        if self.minimum is not None:
            return f">= {self.minimum}"
        return f"<= {self.maximum}"


class RegressionBounds(BaseModel):
    """When a difference between two reports is worth calling a regression."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latency_p95_relative_increase: float = Field(gt=0.0)
    latency_p95_absolute_increase_ms: float = Field(gt=0.0)
    total_tokens_p95_relative_increase: float = Field(gt=0.0)
    total_tokens_p95_absolute_increase: float = Field(gt=0.0)


class EvaluationPolicy(BaseModel):
    """The whole policy, plus the hash of the bytes it was loaded from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(min_length=1)
    policy_version: int = Field(ge=1)
    thresholds: dict[str, Threshold] = Field(min_length=1)
    regression: RegressionBounds
    content_sha256: str = Field(min_length=64, max_length=64)

    def applicable(self, mode: ExecutionMode, *, judge_enabled: bool) -> dict[str, Threshold]:
        """The thresholds that decide this run's verdict."""
        return {
            name: threshold
            for name, threshold in self.thresholds.items()
            if threshold.applies_to(mode, judge_enabled=judge_enabled)
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict. Carries the observed value, or its absence."""

    metric: str
    status: GateStatus
    bound: str
    observed: float | None
    rationale: str

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASS


def load_policy(path: Path | None = None) -> EvaluationPolicy:
    """Load, validate and hash the evaluation policy.

    Raises:
        ConfigurationError: if the file is missing, malformed, or any threshold
            is not a single well-formed bound.
    """
    policy_path = DEFAULT_POLICY_PATH if path is None else path

    try:
        raw_bytes = policy_path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(
            f"Evaluation policy could not be read: {policy_path.name}"
        ) from exc

    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{policy_path.name} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"{policy_path.name} must contain a JSON object.")

    payload = {key: value for key, value in raw.items() if not key.startswith("$")}
    regression = payload.get("regression")
    if isinstance(regression, dict):
        payload["regression"] = {
            key: value for key, value in regression.items() if not key.startswith("$")
        }
    payload["content_sha256"] = hashlib.sha256(raw_bytes).hexdigest()

    try:
        return EvaluationPolicy.model_validate(payload)
    except ValidationError as exc:
        locations = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise ConfigurationError(
            f"{policy_path.name} failed schema validation at: {', '.join(locations)}"
        ) from exc


def evaluate_gates(
    policy: EvaluationPolicy,
    observed: dict[str, float | None],
    mode: ExecutionMode,
    *,
    judge_enabled: bool,
) -> list[GateResult]:
    """Apply every applicable threshold. Deterministic and side-effect free.

    `observed` maps metric name to value, where `None` means "not measurable in
    this run". A `None` — or a metric absent from the mapping entirely — yields
    INCOMPLETE, never PASS.
    """
    results: list[GateResult] = []
    for name in sorted(policy.applicable(mode, judge_enabled=judge_enabled)):
        threshold = policy.thresholds[name]
        value = observed.get(name)
        if value is None:
            status = GateStatus.INCOMPLETE
        else:
            status = GateStatus.PASS if threshold.satisfied_by(value) else GateStatus.FAIL
        results.append(
            GateResult(
                metric=name,
                status=status,
                bound=threshold.describe(),
                observed=value,
                rationale=threshold.rationale,
            )
        )
    return results


def overall_status(results: list[GateResult], *, extra_incomplete: bool = False) -> GateStatus:
    """Combine gate verdicts. FAIL dominates INCOMPLETE, which dominates PASS.

    `extra_incomplete` lets a caller mark a run unusable for a reason no single
    gate can express — a live report produced from a dirty working tree being
    the one that matters here, since such a report cannot be attributed to any
    commit and must never become a baseline.
    """
    if any(result.status is GateStatus.FAIL for result in results):
        return GateStatus.FAIL
    if extra_incomplete or any(result.status is GateStatus.INCOMPLETE for result in results):
        return GateStatus.INCOMPLETE
    return GateStatus.PASS


__all__ = [
    "EvaluationPolicy",
    "ExecutionMode",
    "GateResult",
    "GateStatus",
    "RegressionBounds",
    "Threshold",
    "evaluate_gates",
    "load_policy",
    "overall_status",
]
