"""One bounded LLM judge. Evaluation only — never runtime authority.

WHAT THIS JUDGE IS NOT
----------------------
It is not part of tool selection, policy, evidence classification or response
production. It reads diagnoses the deterministic agent has ALREADY produced and
grades them. If this file were deleted the agent would behave identically.

That boundary is the reason a judge is acceptable here at all. A model that
decided whether a cause was supported would be the same class of system whose
output is being checked, vulnerable to the same injection, and the product's
central claim would rest on it. A model that *reviews* a finished, deterministic
answer adds a second opinion without acquiring any authority.

WHY ONLY THREE CASES
--------------------
The three that carry the distinction the product exists to make:

    supported_drift            a cause the evidence and runbook support
    correlation_only_drift     the SAME metrics without the mechanism
    missing_monitoring_tables  no evidence at all — abstain

A judge that cannot separate those three cannot say anything useful about the
other sixteen, and running it on all nineteen would spend nineteen metered
calls to learn what three can tell us.

CALL BUDGET
-----------
One call per case, three total, hard-capped at six by `CallBudget`. The cap is
enforced in code rather than trusted to configuration, because "it should only
call once per case" is an assumption about a library, not a fact about it.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from mlflow.entities import Feedback
from mlflow.genai.judges import make_judge
from mlflow.genai.scorers import scorer

#: The endpoint. Sonnet rather than Opus: this is a bounded rubric check on a
#: short structured answer, and Opus's cost is not justified by the task.
JUDGE_MODEL = "databricks:/databricks-claude-sonnet-5"

JUDGE_NAME = "evidence_discipline"

#: The three cases. Not configurable — widening the dataset is a decision, not
#: a parameter.
JUDGED_CASES = ("supported-drift", "correlation-only-drift", "missing-monitoring-tables")

#: Hard ceiling. Three expected, six allowed for a transient transport retry.
MAX_ENDPOINT_CALLS = 6

Verdict = Literal["meets_expectations", "partially_meets", "does_not_meet"]

RUBRIC = """\
You are reviewing a diagnosis produced by a deterministic, read-only ML
operations agent. The agent has already decided; you are grading whether its
answer respects six rules. You are NOT diagnosing the model yourself, and you
must not suggest what the agent should have concluded.

The agent's answer is in {{ outputs }}. The request is in {{ inputs }}.

Grade the answer against these six rules:

1. DEGRADATION REQUIRES EVIDENCE. `degradation_status` is "degraded" only if
   the answer cites a monitoring time series AND a threshold breach. If the
   monitoring source is absent, the status must be "insufficient_evidence".

2. A SUPPORTED CAUSE NEEDS A MECHANISM. Any entry in `likely_causes` must have
   support="supported", cite at least one evidence reference, and name a
   runbook mechanism. A cause resting only on two metrics moving together does
   not qualify.

3. CORRELATION IS NOT CAUSATION. Co-movement must appear under
   `recommended_investigations` phrased as something to confirm, never asserted
   in `likely_causes`.

4. ABSENCE MEANS ABSTAIN. If monitoring history does not exist, the status is
   "insufficient_evidence" and `limitations` must say what was missing. It must
   NOT be reported as healthy.

5. NOTHING WAS EXECUTED. The answer must not state or imply that any action was
   taken, or recommend that the agent itself perform one. It is read-only.

6. EVIDENCE IS SEPARATE FROM INFERENCE. Citations in `supporting_evidence` are
   observations; `likely_causes` and `recommended_investigations` are
   inference. The answer must not blur them.

Return exactly one of:
  meets_expectations  - all six rules are respected
  partially_meets     - no rule is violated outright, but one is only weakly met
  does_not_meet       - at least one rule is violated

Give a one-sentence rationale naming the rule numbers that drove your verdict.
"""


class CallBudget:
    """A hard cap on endpoint calls, enforced rather than assumed.

    `mlflow.genai.evaluate` decides how often it invokes a scorer. "One call
    per case" is a belief about a library, and this turns it into a fact about
    this run: the seventh call raises instead of happening.
    """

    def __init__(self, limit: int = MAX_ENDPOINT_CALLS) -> None:
        self.limit = limit
        self.used = 0

    def spend(self) -> None:
        if self.used >= self.limit:
            raise RuntimeError(
                f"endpoint call budget of {self.limit} is exhausted; refusing a further call"
            )
        self.used += 1


BUDGET = CallBudget()


def build_judge() -> Any:
    """The judge, with a categorical structured output."""
    return make_judge(
        name=JUDGE_NAME,
        instructions=RUBRIC,
        model=JUDGE_MODEL,
        description=(
            "Grades whether a finished diagnosis respects the product's evidence "
            "discipline. Evaluation only; no runtime authority."
        ),
        feedback_value_type=Verdict,
    )


@scorer(name=JUDGE_NAME)
def evidence_discipline(inputs: Any, outputs: Any) -> Feedback:
    """Invoke the judge for one case, under the call budget.

    Wrapped in a `@scorer` rather than passing the judge directly so the budget
    is enforced at the only place a call can originate, and so a judge failure
    becomes a recorded verdict rather than an aborted evaluation.
    """
    BUDGET.spend()
    judge = build_judge()
    try:
        verdict = judge(inputs=inputs, outputs=outputs)
        if not isinstance(verdict, Feedback):
            # Fails closed on an unexpected judge return shape rather than
            # passing something unreadable through as a result.
            return Feedback(
                value="does_not_meet",
                rationale="the judge returned an unrecognised result shape",
            )
        return verdict
    except Exception as error:
        # Sanitised: a provider error can carry an endpoint URL or a request id.
        return Feedback(
            value="does_not_meet",
            rationale=f"the judge could not be evaluated ({type(error).__name__})",
        )


def dry_run(experiment: str) -> dict[str, Any]:
    """What the judged run WOULD do. Creates and calls nothing."""
    return {
        "experiment": experiment,
        "endpoint": JUDGE_MODEL,
        "case_ids": list(JUDGED_CASES),
        "judge_name": JUDGE_NAME,
        "scorer_names": [JUDGE_NAME],
        "expected_endpoint_calls": len(JUDGED_CASES),
        "maximum_endpoint_calls": MAX_ENDPOINT_CALLS,
        "concurrency": 1,
        "async_trace_export": "disabled",
        "verdict_values": ["meets_expectations", "partially_meets", "does_not_meet"],
        "inputs": "synthetic, sanitised — the same fixtures as the deterministic run",
        "predicted_remote_records": [
            "one evaluation run in the existing experiment",
            f"{len(JUDGED_CASES)} root traces with nested agent/tool spans",
            f"{len(JUDGED_CASES)} judge assessments",
        ],
        "will_not_modify": [
            "the existing deterministic run fdf2a71507234bc69fc088c8775c333f",
            "the failed run 45c93cfe68044d8d972eefb0db37b036",
            "any Unity Catalog object",
            "any SQL warehouse (none is started or queried)",
        ],
    }


def render(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2)


__all__ = [
    "BUDGET",
    "JUDGED_CASES",
    "JUDGE_MODEL",
    "JUDGE_NAME",
    "MAX_ENDPOINT_CALLS",
    "RUBRIC",
    "CallBudget",
    "build_judge",
    "dry_run",
    "evidence_discipline",
    "render",
]
