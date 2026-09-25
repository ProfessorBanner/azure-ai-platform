"""The evidence rules. Pure functions; no I/O, no clock, no randomness.

This module is where "degraded" and "supported" are DEFINED. Everything else
either gathers evidence or renders a decision this module already made, which
is what makes the product's central claims testable without a workspace.

THE DEGRADATION GATE
--------------------
`permits_degradation_finding` returns a reason whenever a degradation finding is
NOT permitted, and None when it is. Four conditions must all hold:

  1. a monitoring time series exists;
  2. the requested interval is covered by it;
  3. a version-controlled threshold is available for the metric;
  4. the metric crosses that threshold.

Any one missing yields `insufficient_evidence`. Note what is NOT a substitute
for (1): training-time metrics from the registry. Phase 19.2a found all seven
DEV versions carry identical `rmse`/`r2`, so registry metrics cannot separate a
degraded model from a healthy one even in principle. A gate that accepted them
would answer the question with data that has no bearing on it.

COVERAGE IS NOT "SOME ROWS EXIST"
---------------------------------
A seven-day question answered from one row at the far end of the window is a
worse failure than no answer, because it looks like an answer. `covers_window`
requires observations near both ends and a gap no larger than
`MAX_OBSERVATION_GAP_DAYS`. A stale pipeline that stopped writing four days ago
does not cover a seven-day window, and saying so is the point of scenario 12.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import timedelta

from ml_platform_operations_agent.domain import (
    CauseSupport,
    DegradationStatus,
    EvidenceReference,
    LikelyCause,
    MetricThreshold,
    MonitoringObservation,
    TimeWindow,
)

#: The largest gap between consecutive observations that still counts as
#: covering a window. Phase 15's monitor runs daily, so two days tolerates one
#: missed run; three consecutive misses is a pipeline problem an operator should
#: be told about rather than averaged over.
MAX_OBSERVATION_GAP_DAYS = 2.0

#: How close to each window edge an observation must fall. Same reasoning: with
#: a daily monitor, nothing within two days of the start means the early part of
#: the question is simply unobserved.
MAX_EDGE_DISTANCE_DAYS = 2.0

#: Metrics whose breach direction is known from the Phase 15 monitoring
#: contract. Used to reject a threshold whose direction contradicts the source.
BREACH_ABOVE_METRICS = frozenset({"rmse", "drift_score", "null_rate"})
BREACH_BELOW_METRICS = frozenset({"r2"})

#: The one blocking reason that means "measured, and healthy" rather than
#: "could not tell". A sentinel rather than a repeated literal: `classify_status`
#: maps exactly this reason to NOT_DEGRADED, and a typo in a duplicated string
#: would silently turn a healthy verdict into an abstention.
NO_BREACH = "no monitored metric crosses its version-controlled threshold in this window"


class CoverageProblem(str):
    """A human-readable reason a window is not covered. A `str` subclass so it
    can flow straight into `Diagnosis.limitations` without a conversion step."""


def _finite(value: float | None) -> bool:
    return value is not None and not math.isnan(value) and not math.isinf(value)


def observations_in_window(
    observations: Sequence[MonitoringObservation],
    window: TimeWindow,
) -> tuple[MonitoringObservation, ...]:
    """Filter to the window and sort by time.

    Sorted so downstream logic is deterministic regardless of the order the
    adapter returned rows in — a SQL result set has no guaranteed order, and
    19.2d gates determinism at 100%.
    """
    inside = [row for row in observations if window.covers(row.observed_at)]
    return tuple(sorted(inside, key=lambda row: (row.observed_at, row.model_version)))


def covers_window(
    observations: Sequence[MonitoringObservation],
    window: TimeWindow,
) -> CoverageProblem | None:
    """None when the series adequately covers the window; a reason otherwise."""
    inside = observations_in_window(observations, window)

    if not inside:
        return CoverageProblem("no monitoring observations fall inside the requested window")

    if len(inside) == 1:
        return CoverageProblem(
            "only one monitoring observation falls inside the requested window, "
            "which cannot establish a trend"
        )

    start_gap = (inside[0].observed_at - window.start).total_seconds() / 86400.0
    end_gap = (window.end - inside[-1].observed_at).total_seconds() / 86400.0

    if start_gap > MAX_EDGE_DISTANCE_DAYS:
        return CoverageProblem(
            f"the first observation is {start_gap:.1f} days after the window start; "
            f"the earliest {start_gap:.1f} days of the window are unobserved"
        )

    if end_gap > MAX_EDGE_DISTANCE_DAYS:
        return CoverageProblem(
            f"the most recent observation is {end_gap:.1f} days before the window end; "
            "monitoring may have stopped"
        )

    for earlier, later in zip(inside, inside[1:], strict=False):
        gap = (later.observed_at - earlier.observed_at).total_seconds() / 86400.0
        if gap > MAX_OBSERVATION_GAP_DAYS:
            return CoverageProblem(
                f"there is a {gap:.1f}-day gap between consecutive observations, "
                "so the window is not continuously observed"
            )

    return None


def metric_values(
    observations: Sequence[MonitoringObservation],
    metric: str,
) -> tuple[tuple[MonitoringObservation, float], ...]:
    """Extract finite values of one metric, paired with the row they came from.

    NaN, infinity and None are dropped rather than coerced. A dropped value is
    an unmeasured point; a coerced one is a fabricated measurement, and the
    difference is exactly the failure this product exists to prevent.
    """
    pairs: list[tuple[MonitoringObservation, float]] = []
    for row in observations:
        value = getattr(row, metric, None)
        if isinstance(value, int | float) and _finite(float(value)):
            pairs.append((row, float(value)))
    return tuple(pairs)


def permits_degradation_finding(
    observations: Sequence[MonitoringObservation],
    window: TimeWindow,
    thresholds: Sequence[MetricThreshold],
) -> str | None:
    """None when a `degraded` verdict is permitted; the blocking reason otherwise.

    Deliberately returns the reason rather than a bool. The reason becomes a
    `limitations` entry, so an operator learns WHY the agent would not commit —
    which is the difference between a useful abstention and a shrug.
    """
    if not observations:
        return "no monitoring time series is available for this model"

    coverage = covers_window(observations, window)
    if coverage is not None:
        return str(coverage)

    if not thresholds:
        return "no version-controlled threshold is available, so no metric can be judged"

    inside = observations_in_window(observations, window)

    # AN UNMEASURED MODEL IS NOT A HEALTHY ONE.
    #
    # `metric_values` drops NaN and infinity, so a series whose metrics are all
    # NaN reaches the breach loop with nothing to compare and would fall through
    # to "no metric crossed its threshold" — reporting an unmeasured model as
    # healthy. That is the dangerous direction of this whole product: an
    # operator reading `not_degraded` stops looking.
    #
    # So the presence of at least one finite, judgeable value is required
    # BEFORE any conclusion about breaching is drawn.
    judgeable = any(metric_values(inside, threshold.metric) for threshold in thresholds)
    if not judgeable:
        return (
            "monitoring rows exist but carry no finite value for any thresholded metric, "
            "so no metric can be judged"
        )

    for threshold in thresholds:
        for _row, value in metric_values(inside, threshold.metric):
            if threshold.is_breached_by(value):
                return None

    return NO_BREACH


def breaching_observations(
    observations: Sequence[MonitoringObservation],
    window: TimeWindow,
    thresholds: Sequence[MetricThreshold],
) -> tuple[tuple[MonitoringObservation, MetricThreshold, float], ...]:
    """Every (row, threshold, value) triple where a threshold is crossed.

    Ordered by time then metric name so the output is stable.
    """
    inside = observations_in_window(observations, window)
    hits: list[tuple[MonitoringObservation, MetricThreshold, float]] = []
    for threshold in thresholds:
        for row, value in metric_values(inside, threshold.metric):
            if threshold.is_breached_by(value):
                hits.append((row, threshold, value))
    return tuple(sorted(hits, key=lambda hit: (hit[0].observed_at, hit[1].metric)))


def threshold_direction_is_consistent(threshold: MetricThreshold) -> bool:
    """Guard against a threshold whose direction contradicts Phase 15.

    A `breach_above=True` threshold on `r2` would mean "degraded when the model
    explains MORE variance", which inverts every verdict that depends on it.
    Unknown metrics pass: this checks the metrics whose semantics are fixed by
    the monitoring contract, and is not an allow-list.
    """
    if threshold.metric in BREACH_ABOVE_METRICS:
        return threshold.breach_above
    if threshold.metric in BREACH_BELOW_METRICS:
        return not threshold.breach_above
    return True


def classify_status(
    observations: Sequence[MonitoringObservation],
    window: TimeWindow,
    thresholds: Sequence[MetricThreshold],
) -> tuple[DegradationStatus, tuple[str, ...]]:
    """The verdict, plus any limitations that qualify it.

    Three-way and total: every input maps to exactly one status.
    """
    blocking = permits_degradation_finding(observations, window, thresholds)

    if blocking is None:
        return DegradationStatus.DEGRADED, ()

    # A clean series that simply has no breach is NOT_DEGRADED — a real answer.
    # Anything else is an absence of evidence, which is not the same thing and
    # must not be reported as health.
    if blocking == NO_BREACH:
        return DegradationStatus.NOT_DEGRADED, ()

    return DegradationStatus.INSUFFICIENT_EVIDENCE, (blocking,)


def demote_unsupported_causes(
    causes: Sequence[LikelyCause],
) -> tuple[tuple[LikelyCause, ...], tuple[str, ...]]:
    """Split causes into those that may be asserted and those that may not.

    A cause is only assertable when it is SUPPORTED. Everything else becomes a
    recommended investigation, phrased as a question to answer rather than a
    finding — which is the required treatment of metric co-movement.

    The `LikelyCause` model already refuses to construct a SUPPORTED cause
    without evidence and a mechanism, so this function never needs to re-check
    that. It is the routing step, not a second gate.
    """
    assertable: list[LikelyCause] = []
    investigations: list[str] = []

    for cause in causes:
        if cause.support is CauseSupport.SUPPORTED:
            assertable.append(cause)
        elif cause.support is CauseSupport.CORRELATION_ONLY:
            investigations.append(
                f"Confirm or rule out: {cause.statement} "
                "(observed as co-movement only; no mechanism established)"
            )
        else:
            investigations.append(f"Consider: {cause.statement} (not supported by this evidence)")

    return tuple(assertable), tuple(investigations)


def confidence_for(
    status: DegradationStatus,
    supported_causes: Sequence[LikelyCause],
    references: Sequence[EvidenceReference],
) -> float:
    """A deterministic, explainable confidence. Not a probability.

    Deliberately a small arithmetic ladder rather than anything learned: this
    number is read by an operator deciding whether to act at 3am, and a score
    nobody can reconstruct is a score nobody should trust.

    Capped at 0.5 for `insufficient_evidence` because `Diagnosis` refuses to
    hold more than that — high confidence in an absence is the exact failure
    mode the product exists to prevent.
    """
    if status is DegradationStatus.INSUFFICIENT_EVIDENCE:
        # Slightly above the floor when we at least know which sources were
        # absent, which is more useful than knowing nothing at all.
        return 0.2 if references else 0.1

    base = 0.5
    base += 0.2 if supported_causes else 0.0
    base += min(len(references), 3) * 0.05
    return round(min(base, 0.95), 4)


def elapsed_days(window: TimeWindow) -> float:
    """Window length in days. Used by tools to describe their query window."""
    return window.duration_days


def window_ending_at(end: object, days: int) -> TimeWindow:
    """Build a `days`-long window ending at `end`.

    Takes the end explicitly rather than reading a clock: a function that called
    `datetime.now()` would make every diagnosis non-reproducible, and 19.2d
    gates deterministic output at 100%.
    """
    from datetime import datetime as _dt

    if not isinstance(end, _dt):
        raise TypeError("window_ending_at requires a datetime")
    return TimeWindow(start=end - timedelta(days=days), end=end)


__all__ = [
    "BREACH_ABOVE_METRICS",
    "BREACH_BELOW_METRICS",
    "MAX_EDGE_DISTANCE_DAYS",
    "MAX_OBSERVATION_GAP_DAYS",
    "NO_BREACH",
    "CoverageProblem",
    "breaching_observations",
    "classify_status",
    "confidence_for",
    "covers_window",
    "demote_unsupported_causes",
    "elapsed_days",
    "metric_values",
    "observations_in_window",
    "permits_degradation_finding",
    "threshold_direction_is_consistent",
    "window_ending_at",
]
