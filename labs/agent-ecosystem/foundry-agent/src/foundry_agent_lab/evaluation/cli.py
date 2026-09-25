"""Command line entry point for the lab evaluation.

    uv run --directory labs/agent-ecosystem/foundry-agent \
      python -m foundry_agent_lab.evaluation.cli --mode fake

Exit codes match the Phase 18 suites, and for the same reason:

    0  every applicable gate passed
    1  a gate failed
    2  incomplete — a required metric could not be computed
    3  misconfiguration

LIVE IS NEVER THE DEFAULT. `--mode live` calls a metered deployment and is a
deliberate, human-invoked command.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from foundry_agent_lab._product import ensure_product_importable
from foundry_agent_lab.docs_search import _product_search_tool
from foundry_agent_lab.evaluation.dataset import GATES_PATH, DatasetError, load_dataset
from foundry_agent_lab.evaluation.metrics import compute_metrics
from foundry_agent_lab.evaluation.report import ReportInputs, render_markdown, write_reports
from foundry_agent_lab.evaluation.runner import run_dataset
from foundry_agent_lab.governance import GovernedToolGateway

ensure_product_importable()

from platform_engineering_assistant.evaluation.policy import (  # noqa: E402
    ExecutionMode,
    GateStatus,
    evaluate_gates,
    load_policy,
    overall_status,
)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCOMPLETE = 2
EXIT_CONFIGURATION = 3

DEFAULT_OUTPUT = Path("artifacts/agent-lab-evaluation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="foundry-agent-lab-eval",
        description="Evaluate the Foundry agent lab against its versioned case set.",
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in ExecutionMode],
        default=ExecutionMode.FAKE.value,
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--agent-version", default="eval-1")
    arguments = parser.parse_args(argv)
    mode = ExecutionMode(arguments.mode)

    try:
        dataset = load_dataset()
        # The Phase 18 loader, over this lab's gate file: one definition of what
        # a threshold is and what INCOMPLETE means, across both stacks.
        gates_policy = load_policy(GATES_PATH)
    except (DatasetError, Exception) as error:  # noqa: BLE001
        print(f"Evaluation could not start: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION

    if mode is ExecutionMode.LIVE:
        print(
            "Live evaluation requires a provisioned Foundry agent and calls a metered "
            "deployment. Provision first, then supply a live runtime factory; see README.",
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION

    gateway = GovernedToolGateway(_product_search_tool()._index)
    result = run_dataset(dataset, gateway)
    metrics = compute_metrics(result.outcomes, result.planned_cases)

    gates = evaluate_gates(gates_policy, metrics.as_observed(), mode, judge_enabled=False)
    applicable = set(gates_policy.applicable(mode, judge_enabled=False))
    unassessed = tuple(sorted(set(gates_policy.thresholds) - applicable))
    status = overall_status(gates, extra_incomplete=not result.complete)

    inputs = ReportInputs(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset.content_sha256,
        gates_id=gates_policy.policy_id,
        gates_version=gates_policy.policy_version,
        execution_mode=mode.value,
        agent_name="eval-agent",
        agent_version=arguments.agent_version,
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        metrics=metrics,
        gates=gates,
        outcomes=result.outcomes,
        status=status.value,
        unassessed=unassessed,
        limitations=dataset.limitations,
    )

    directory = Path(arguments.output) if arguments.output else DEFAULT_OUTPUT
    json_path, markdown_path = write_reports(inputs, directory)
    print(render_markdown(inputs))
    print(f"\nreport: {json_path}\nsummary: {markdown_path}")

    if status is GateStatus.FAIL:
        return EXIT_FAIL
    if status is GateStatus.INCOMPLETE:
        return EXIT_INCOMPLETE
    return EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
