"""Orchestration: run every case the configured number of times and record it.

The runner owns two responsibilities and no more: deciding what to call, and
recording what happened. It does not score (that is `metrics`) and it does not
render (that is `report`).

NO RETRIES. Each (case, repetition) is attempted exactly once. If a call fails,
that failure IS the datum — re-running it until it succeeds would turn an error
rate into a measure of patience and quietly change every denominator. Repetition
is for measuring consistency, not for hiding failures.

PACING, NOT RETRYING. A deployment has a request rate limit. Attempts are
therefore separated by a caller-supplied delay. Pacing changes only the spacing
of calls; it never changes how many are made, so no metric denominator moves.

ABORT ON STRUCTURAL FAILURE. Some failures mean every remaining call will fail
the same way: the token was rejected, the role is missing, the address is not
allow-listed, the endpoint is wrong, or the deployment is rate limiting. Grinding
through the remaining cases would produce nothing but a longer list of identical
errors and, in the rate-limited case, would actively prolong the throttle. The
run stops, is marked operationally INCOMPLETE, and its partial results are kept
as a diagnostic — but never presented as a quality measurement.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from foundry_capability_lab.errors import FailureCategory, LabError
from foundry_capability_lab.evaluation.dataset import EvaluationCase
from foundry_capability_lab.evaluation.metrics import CaseOutcome
from foundry_capability_lab.provider import RiskAssessmentProvider

DEFAULT_REPETITIONS = 3

Sleeper = Callable[[float], None]

# Failures where continuing is pointless and sometimes harmful.
#
# RATE_LIMITED is included deliberately. It is not evidence about the model: it
# is evidence that the deployment's capacity is too small for the run being
# attempted, and every further call extends the throttling window. The correct
# response is to raise capacity or increase the pacing delay and start again,
# not to collect thirty-five more copies of the same error.
#
# INVALID_STRUCTURED_OUTPUT, PROVIDER_ERROR and TIMEOUT are deliberately NOT
# here: a malformed response or a transient 500 is genuine quality and
# reliability evidence, and the run should continue and report it.
ABORTING_FAILURES = frozenset(
    {
        FailureCategory.AUTHENTICATION,
        FailureCategory.AUTHORIZATION,
        FailureCategory.NETWORK_DENIED,
        FailureCategory.CONFIGURATION,
        FailureCategory.RATE_LIMITED,
    }
)


class EvaluationRun(BaseModel):
    """Everything a completed (or aborted) run produced, before scoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcomes: list[CaseOutcome]
    repetitions: int = Field(ge=1)
    planned_attempts: int = Field(ge=0)
    aborted_on: FailureCategory | None = Field(
        default=None,
        description="The failure category that stopped the run; None if it ran to completion.",
    )
    sleeps: int = Field(default=0, ge=0)
    inter_attempt_delay_seconds: float = Field(default=0.0, ge=0)

    @property
    def operationally_complete(self) -> bool:
        """True when every planned attempt was made and nothing aborted the run.

        Only a complete run may be reported as a quality result. An incomplete
        run's metrics are computed over whatever happened to finish, which is not
        a measurement of anything.
        """
        return self.aborted_on is None and len(self.outcomes) == self.planned_attempts


def _score_evidence(actual_ids: list[str], valid_ids: list[str]) -> tuple[bool, int]:
    """Count evidence identifiers the model produced that the case does not support.

    Returns (is_valid, fabricated_count). An empty evidence list is valid: the
    schema permits it, and declining to cite is not fabrication.
    """
    permitted = set(valid_ids)
    fabricated = [identifier for identifier in actual_ids if identifier not in permitted]
    return (not fabricated, len(fabricated))


def run_evaluation(
    provider: RiskAssessmentProvider,
    cases: list[EvaluationCase],
    repetitions: int = DEFAULT_REPETITIONS,
    *,
    inter_attempt_delay_seconds: float = 0.0,
    sleeper: Sleeper = time.sleep,
) -> EvaluationRun:
    """Attempt every case `repetitions` times, pacing and recording each attempt.

    Latency is measured HERE, around the provider call, for every attempt. The
    provider reports its own latency on success only; measuring in the runner
    means failed attempts contribute to the latency distribution too, so p50/p95
    describe every call made rather than only the flattering ones. The pacing
    delay is deliberately measured OUT of latency — it is applied between
    attempts, never inside the timed region.

    `sleeper` is injected so tests can count delays without spending them.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if inter_attempt_delay_seconds < 0:
        raise ValueError("inter_attempt_delay_seconds must not be negative")

    planned = len(cases) * repetitions
    outcomes: list[CaseOutcome] = []
    aborted_on: FailureCategory | None = None
    sleeps = 0

    for repetition in range(1, repetitions + 1):
        if aborted_on is not None:
            break
        for case in cases:
            # Pace BETWEEN attempts only: never before the first, never after
            # the last. N attempts therefore cost exactly N-1 delays.
            if outcomes and inter_attempt_delay_seconds > 0:
                sleeper(inter_attempt_delay_seconds)
                sleeps += 1

            started = time.perf_counter()
            try:
                result = provider.assess(case.observation)
            except LabError as error:
                outcomes.append(
                    CaseOutcome(
                        case_id=case.case_id,
                        repetition=repetition,
                        schema_valid=False,
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                        expected_classification=case.expected_classification,
                        expected_severity=case.expected_severity,
                        expected_requires_escalation=case.expected_requires_escalation,
                        failure_category=error.category,
                    )
                )
                if error.category in ABORTING_FAILURES:
                    aborted_on = error.category
                    break
                continue

            latency_ms = (time.perf_counter() - started) * 1000.0
            assessment = result.assessment
            evidence_valid, fabricated = _score_evidence(
                assessment.evidence_ids, case.valid_evidence_ids
            )

            outcomes.append(
                CaseOutcome(
                    case_id=case.case_id,
                    repetition=repetition,
                    schema_valid=True,
                    latency_ms=latency_ms,
                    actual_classification=assessment.risk_classification,
                    actual_severity=assessment.severity,
                    actual_requires_escalation=assessment.requires_escalation,
                    evidence_valid=evidence_valid,
                    fabricated_evidence_count=fabricated,
                    expected_classification=case.expected_classification,
                    expected_severity=case.expected_severity,
                    expected_requires_escalation=case.expected_requires_escalation,
                    input_tokens=result.telemetry.token_usage.input_tokens,
                    output_tokens=result.telemetry.token_usage.output_tokens,
                    total_tokens=result.telemetry.token_usage.total_tokens,
                )
            )

    return EvaluationRun(
        outcomes=outcomes,
        repetitions=repetitions,
        planned_attempts=planned,
        aborted_on=aborted_on,
        sleeps=sleeps,
        inter_attempt_delay_seconds=inter_attempt_delay_seconds,
    )
