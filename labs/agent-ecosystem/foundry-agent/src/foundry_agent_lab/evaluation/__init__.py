"""Deterministic evaluation for the Foundry agent lab.

Reuses the Phase 18 gate machinery — `ExecutionMode`, `Threshold`,
`evaluate_gates`, `overall_status`, `GateStatus` — rather than restating it, so
both stacks agree on what a gate is and what INCOMPLETE means.
"""

from foundry_agent_lab.evaluation.dataset import LabCase, LabDataset, load_dataset
from foundry_agent_lab.evaluation.metrics import LabCaseOutcome, LabMetrics, compute_metrics
from foundry_agent_lab.evaluation.report import build_report, render_markdown
from foundry_agent_lab.evaluation.runner import run_dataset

__all__ = [
    "LabCase",
    "LabCaseOutcome",
    "LabDataset",
    "LabMetrics",
    "build_report",
    "compute_metrics",
    "load_dataset",
    "render_markdown",
    "run_dataset",
]
