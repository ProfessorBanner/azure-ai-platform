"""Phase 16.3A: application-owned evaluation of the Foundry risk classifier.

The evaluation is deliberately OURS rather than a provider feature. Scoring,
thresholds and reporting live in this repository, in code, under review — so the
question "did this get better or worse" has an answer that does not depend on a
vendor surface, and the same harness can be pointed at a different provider in
Phase 17 without rewriting what "good" means.

The model is non-deterministic; the SCORING is deterministic. Given the same
recorded outcomes, this package always produces the same metrics and the same
verdict.
"""

from foundry_capability_lab.evaluation.dataset import (
    EvaluationCase,
    load_cases,
)
from foundry_capability_lab.evaluation.metrics import (
    CaseOutcome,
    EvaluationMetrics,
    compute_metrics,
    percentile,
)
from foundry_capability_lab.evaluation.report import (
    build_report,
    render_markdown,
    write_reports,
)
from foundry_capability_lab.evaluation.runner import EvaluationRun, run_evaluation
from foundry_capability_lab.evaluation.thresholds import (
    GateResult,
    ThresholdSet,
    evaluate_gates,
    load_thresholds,
)

__all__ = [
    "CaseOutcome",
    "EvaluationCase",
    "EvaluationMetrics",
    "EvaluationRun",
    "GateResult",
    "ThresholdSet",
    "build_report",
    "compute_metrics",
    "evaluate_gates",
    "load_cases",
    "load_thresholds",
    "percentile",
    "render_markdown",
    "run_evaluation",
    "write_reports",
]
