"""Command-line entry point for the risk evaluation.

EXIT CODES
----------
  0  the evaluation ran and every GATED threshold passed
  1  the evaluation ran to completion but a gated threshold failed
  2  configuration or operational failure — no trustworthy result was produced

The distinction between 1 and 2 is the point of this module. Exit 1 is a
RESULT: the harness worked and the model did not clear the bar. Exit 2 means the
harness never got a fair measurement — the dataset would not load, a threshold
names a metric that does not exist, or every call was refused for auth, RBAC or
network reasons. Collapsing them would make a missing role assignment look
identical to a quality regression, and they need completely different responses.

Run:
    uv run python -m foundry_capability_lab.evaluation.cli --provider offline
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from foundry_capability_lab.config import load_config
from foundry_capability_lab.errors import ConfigurationError, LabError
from foundry_capability_lab.evaluation.dataset import DEFAULT_DATASET_PATH, load_cases
from foundry_capability_lab.evaluation.metrics import compute_metrics
from foundry_capability_lab.evaluation.offline import (
    PROVIDER_LABEL,
    OfflineRiskAssessmentProvider,
)
from foundry_capability_lab.evaluation.report import (
    DEFAULT_ARTIFACTS_DIR,
    build_report,
    write_reports,
)
from foundry_capability_lab.evaluation.runner import DEFAULT_REPETITIONS, run_evaluation
from foundry_capability_lab.evaluation.thresholds import (
    DEFAULT_THRESHOLDS_PATH,
    evaluate_gates,
    gates_passed,
    load_thresholds,
)
from foundry_capability_lab.provider import (
    FoundryRiskAssessmentProvider,
    RiskAssessmentProvider,
)

EXIT_PASSED = 0
EXIT_THRESHOLD_FAILED = 1
EXIT_OPERATIONAL_FAILURE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="foundry-evaluation",
        description="Evaluate the Foundry risk classifier against a fixed dataset.",
    )
    parser.add_argument(
        "--provider",
        choices=("foundry", "offline"),
        default="foundry",
        help=(
            "'foundry' calls the real deployment (needs az login, the Foundry User "
            "role and an allow-listed address). 'offline' runs deterministic rules "
            "with no network, to rehearse the harness only — its scores say nothing "
            "about the model."
        ),
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_REPETITIONS,
        help=(
            f"Attempts per case (default {DEFAULT_REPETITIONS}). Repetitions measure "
            "consistency; failed attempts are never retried."
        ),
    )
    parser.add_argument(
        "--inter-attempt-delay-seconds",
        type=float,
        default=None,
        help=(
            "Seconds to wait BETWEEN attempts (never before the first or after "
            "the last). Required and must be > 0 for --provider foundry: the "
            "deployment enforces a request rate limit, and the right delay "
            "depends on its capacity, which this application deliberately does "
            "not assume. Defaults to 0 for --provider offline. Pacing never "
            "changes attempt counts, and there are no retries."
        ),
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS_PATH)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    return parser


def resolve_delay_seconds(provider_name: str, requested: float | None) -> float:
    """Decide the inter-attempt delay, refusing to guess for a live run.

    A live deployment enforces a request rate limit that depends on its
    provisioned capacity. This application deliberately does NOT encode that
    number: baking "wait 6 seconds" into the harness would hide a
    deployment-specific assumption inside application code, and it would silently
    become wrong the moment capacity changed. The operator supplies it, because
    the operator is the one who knows what was provisioned.

    Offline runs default to 0: there is nothing to throttle.

    Raises:
        ConfigurationError: if the value is missing for a live run, or negative.
    """
    if requested is not None and requested < 0:
        raise ConfigurationError("--inter-attempt-delay-seconds must not be negative.")

    if provider_name == "offline":
        return 0.0 if requested is None else requested

    if requested is None:
        raise ConfigurationError(
            "--inter-attempt-delay-seconds is required for --provider foundry. "
            "The deployment enforces a request rate limit set by its capacity, "
            "and this application will not assume a value. Divide 60 by the "
            "deployment's requests-per-minute allowance and add a margin."
        )
    if requested <= 0:
        raise ConfigurationError(
            "--inter-attempt-delay-seconds must be greater than zero for "
            "--provider foundry. An unpaced live run collides with the "
            "deployment's rate limit and measures the throttle, not the model."
        )
    return requested


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.repetitions < 1:
        print("ERROR: --repetitions must be at least 1.", file=sys.stderr)
        return EXIT_OPERATIONAL_FAILURE

    # Pacing is resolved BEFORE any provider is constructed and before any call,
    # so a missing or nonsensical delay costs nothing and leaks no token.
    try:
        delay_seconds = resolve_delay_seconds(args.provider, args.inter_attempt_delay_seconds)
    except LabError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL_FAILURE

    # --- setup: any failure here means no measurement was possible ----------
    try:
        cases = load_cases(args.dataset)
        thresholds = load_thresholds(args.thresholds)

        provider: RiskAssessmentProvider
        if args.provider == "offline":
            provider = OfflineRiskAssessmentProvider()
            deployment = PROVIDER_LABEL
            provider_label = PROVIDER_LABEL
        else:
            config = load_config()
            provider = FoundryRiskAssessmentProvider.from_config(config)
            deployment = config.deployment
            provider_label = "foundry"
    except LabError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL_FAILURE

    # --- run ----------------------------------------------------------------
    run = run_evaluation(
        provider,
        cases,
        repetitions=args.repetitions,
        inter_attempt_delay_seconds=delay_seconds,
    )

    if run.aborted_on is not None:
        print(
            f"ABORTED on '{run.aborted_on}' after {len(run.outcomes)} of "
            f"{run.planned_attempts} planned attempts. Remaining calls were "
            "skipped because every one of them would have failed the same way. "
            "A partial diagnostic report has been written; it is not a quality "
            "result.",
            file=sys.stderr,
        )

    # --- score and report ---------------------------------------------------
    # Metrics are computed even for an aborted run: the partial record is the
    # diagnostic. The report marks it incomplete so it cannot be misread.
    metrics = compute_metrics(run.outcomes)
    try:
        gate_results = evaluate_gates(metrics, thresholds)
    except LabError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL_FAILURE

    report = build_report(
        metrics,
        gate_results,
        run.outcomes,
        deployment=deployment,
        dataset_name=args.dataset.name,
        threshold_version=thresholds.version,
        provider_label=provider_label,
        operationally_complete=run.operationally_complete,
        aborted_on=str(run.aborted_on) if run.aborted_on else None,
        planned_attempts=run.planned_attempts,
        inter_attempt_delay_seconds=delay_seconds,
    )

    try:
        json_path, markdown_path = write_reports(report, args.artifacts_dir)
    except OSError as error:
        print(f"ERROR: reports could not be written: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL_FAILURE

    passed = gates_passed(gate_results)
    complete = run.operationally_complete

    if not complete:
        headline = "INCOMPLETE"
    else:
        headline = "PASSED" if passed else "FAILED"

    print(f"Evaluation {headline}")
    print(
        f"  {metrics.cases} cases x {metrics.repetitions} repetitions = "
        f"{metrics.attempts} of {run.planned_attempts} attempts, "
        f"{metrics.valid_outputs} schema-valid, {run.sleeps} paced waits"
    )
    if complete:
        for gate in gate_results:
            if gate.gated and not gate.passed:
                print(f"  gate FAILED: {gate.metric}={gate.value:.3f} (needs {gate.bound})")
    print(f"  report: {json_path}")
    print(f"  report: {markdown_path}")

    # An incomplete run is never a quality verdict, whatever the partial numbers
    # happen to say.
    if not complete:
        return EXIT_OPERATIONAL_FAILURE
    return EXIT_PASSED if passed else EXIT_THRESHOLD_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
