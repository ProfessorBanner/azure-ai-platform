"""Offline and manual measurement of this product's quality.

Two layers, deliberately separate:

  RETRIEVAL (Phase 17.1b) — `retrieval_baseline`, `calibration`, `validation`.
  Measures the half of the system a language model cannot rescue. No model,
  no network, no Azure.

  GENERATION / END-TO-END (Phase 17.2) — everything else here. Runs the
  application's own answering service over a versioned golden dataset, applies
  deterministic evaluators and an optional structured judge, and gates the
  result against a versioned policy.

The generation suite runs offline against a deterministic provider by default.
Live Azure evaluation is a manual command and is never invoked by a test or a
pipeline.
"""

from platform_engineering_assistant.evaluation.generation_dataset import (
    Dataset,
    GenerationCase,
    load_dataset,
)
from platform_engineering_assistant.evaluation.generation_metrics import (
    CaseOutcome,
    GenerationMetrics,
    compute_metrics,
)
from platform_engineering_assistant.evaluation.generation_runner import RunResult, run_dataset
from platform_engineering_assistant.evaluation.policy import (
    EvaluationPolicy,
    ExecutionMode,
    GateStatus,
    evaluate_gates,
    load_policy,
    overall_status,
)
from platform_engineering_assistant.evaluation.retrieval_baseline import (
    BaselineReport,
    RetrievalCase,
    load_cases,
    measure_baseline,
)

__all__ = [
    "BaselineReport",
    "CaseOutcome",
    "Dataset",
    "EvaluationPolicy",
    "ExecutionMode",
    "GateStatus",
    "GenerationCase",
    "GenerationMetrics",
    "RetrievalCase",
    "RunResult",
    "compute_metrics",
    "evaluate_gates",
    "load_cases",
    "load_dataset",
    "load_policy",
    "measure_baseline",
    "overall_status",
    "run_dataset",
]
