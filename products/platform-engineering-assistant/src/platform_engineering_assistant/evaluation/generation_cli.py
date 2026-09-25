"""Commands for the generation / end-to-end evaluation suite.

    run     evaluate the dataset and write a report
    compare diff two reports

TWO MODES, ONE OF THEM DELIBERATELY EXPENSIVE
---------------------------------------------
`--mode fake` uses the deterministic provider: no network, no credential, no
Azure, no cost. It is what tests and CI run, and it proves the PIPELINE —
schema validity, citation containment, prohibited content, provider failures,
reporting and gating.

`--mode live` calls the real deployment and is a MANUAL command. It is never run
by a test and never by a pipeline. That is a cost-control decision as much as a
security one: sixteen questions, optionally doubled by the judge, against a
deployment with capacity 1.

Exit codes: 0 for a passing gate, 1 for a failing one, 2 for an incomplete run,
and 3 for a configuration error. INCOMPLETE gets its own code because "we did
not measure this" needs to be distinguishable from "we measured it and it was
bad" by anything reading the exit status.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from platform_engineering_assistant.answering import build_service
from platform_engineering_assistant.config import (
    DEFAULT_RETRIEVAL_CONFIG_PATH,
    load_retrieval_config,
)
from platform_engineering_assistant.errors import AssistantError
from platform_engineering_assistant.evaluation.compare import (
    compare_reports,
    load_report,
    render_comparison,
)
from platform_engineering_assistant.evaluation.generation_dataset import load_dataset
from platform_engineering_assistant.evaluation.generation_metrics import compute_metrics
from platform_engineering_assistant.evaluation.generation_report import (
    DEFAULT_ARTIFACTS_DIR,
    ReportInputs,
    write_reports,
)
from platform_engineering_assistant.evaluation.generation_runner import run_dataset
from platform_engineering_assistant.evaluation.judge import (
    JudgeProvider,
    load_judge_prompt,
)
from platform_engineering_assistant.evaluation.policy import (
    ExecutionMode,
    GateStatus,
    evaluate_gates,
    load_policy,
    overall_status,
)
from platform_engineering_assistant.evaluation.provenance import (
    RunProvenance,
    git_sha,
    sha256_of,
    utc_now_iso,
    working_tree_is_dirty,
)
from platform_engineering_assistant.generation.protocol import GenerationProvider

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCOMPLETE = 2
EXIT_CONFIGURATION = 3

# A live run against a capacity-1 deployment. Pacing, never retrying.
DEFAULT_LIVE_DELAY_SECONDS = 2.0


def _build_providers(
    mode: ExecutionMode, *, judge_enabled: bool
) -> tuple[GenerationProvider, JudgeProvider | None, str | None]:
    """Construct the answering provider and, when asked, the judge.

    The Azure imports live inside the live branch so that the fake path — the one
    tests and CI take — imports no Azure SDK at all and cannot accidentally
    acquire a credential.
    """
    if mode is ExecutionMode.FAKE:
        from platform_engineering_assistant.generation.fake import FakeGenerationProvider

        return FakeGenerationProvider(), None, None

    from platform_engineering_assistant.evaluation.judge import AzureOpenAIJudge
    from platform_engineering_assistant.generation.azure_openai import (
        AzureOpenAIGenerationProvider,
    )
    from platform_engineering_assistant.provider_config import load_provider_config

    config = load_provider_config()
    provider = AzureOpenAIGenerationProvider.from_config(config)
    judge = AzureOpenAIJudge.from_config(config) if judge_enabled else None
    return provider, judge, config.deployment


def run_command(arguments: argparse.Namespace) -> int:
    """Evaluate the dataset and write a report."""
    mode = ExecutionMode(arguments.mode)
    judge_enabled = bool(arguments.judge)

    if judge_enabled and mode is ExecutionMode.FAKE:
        print(
            "The semantic judge requires --mode live. There is no offline judge, "
            "because a deterministic stub cannot estimate semantic support and a "
            "number that looked like one would be worse than none.",
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION

    try:
        dataset = load_dataset()
        policy = load_policy()
        provider, judge, deployment = _build_providers(mode, judge_enabled=judge_enabled)
        service = build_service(provider)
        judge_prompt = load_judge_prompt() if judge_enabled else None
    except AssistantError as error:
        print(f"Evaluation could not start: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION

    delay = (
        arguments.delay_seconds
        if arguments.delay_seconds is not None
        else (DEFAULT_LIVE_DELAY_SECONDS if mode is ExecutionMode.LIVE else 0.0)
    )

    result = run_dataset(
        service,
        dataset,
        judge=judge,
        judge_rubric=judge_prompt.text if judge_prompt else "",
        inter_case_delay_seconds=delay,
    )

    metrics = compute_metrics(result.outcomes)
    retrieval_config = load_retrieval_config()

    provenance = RunProvenance(
        git_sha=git_sha(),
        git_dirty=working_tree_is_dirty(),
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset.content_sha256,
        corpus_version=service.corpus_version,
        retrieval_config_version=retrieval_config.version,
        retrieval_config_sha256=sha256_of(DEFAULT_RETRIEVAL_CONFIG_PATH),
        prompt_version=service.prompt.version,
        prompt_sha256=service.prompt.content_hash,
        judge_prompt_version=judge_prompt.version if judge_prompt else None,
        judge_prompt_sha256=judge_prompt.content_hash if judge_prompt else None,
        provider=service.provider_name,
        deployment=result.observed_deployment or deployment,
        model=result.observed_model,
        evaluation_policy_id=policy.policy_id,
        evaluation_policy_version=policy.policy_version,
        evaluation_policy_sha256=policy.content_sha256,
        generated_at=utc_now_iso(),
        case_count=len(dataset.cases),
        execution_mode=mode.value,
        judge_enabled=judge_enabled,
    )

    gates = evaluate_gates(policy, metrics.gate_values(), mode, judge_enabled=judge_enabled)

    incomplete_reasons: list[str] = []
    if provenance.unattributable_live_run:
        incomplete_reasons.append(
            "Live run from a dirty or unidentifiable working tree: the result "
            "cannot be attributed to a commit and must not become a baseline."
        )
    if not result.operationally_complete:
        incomplete_reasons.append(
            f"Run aborted on `{result.aborted_on}` after {len(result.outcomes)} of "
            f"{result.planned_cases} cases."
        )
    if judge_enabled and metrics.judge_failures:
        incomplete_reasons.append(
            f"The judge failed on {metrics.judge_failures} case(s); semantic metrics "
            "are computed over a smaller population than the run."
        )

    status = overall_status(gates, extra_incomplete=bool(incomplete_reasons))

    inputs = ReportInputs(
        provenance=provenance,
        metrics=metrics,
        gates=gates,
        outcomes=result.outcomes,
        status=status,
        operationally_complete=result.operationally_complete,
        aborted_on=result.aborted_on.value if result.aborted_on else None,
        dataset_limitations=dataset.limitations,
        incomplete_reasons=tuple(incomplete_reasons),
    )

    directory = Path(arguments.output) if arguments.output else DEFAULT_ARTIFACTS_DIR
    json_path, markdown_path = write_reports(inputs, directory)

    print(f"Evaluation ({mode.value}{', judged' if judge_enabled else ''}): {status.value.upper()}")
    print(f"  cases={metrics.cases} completed={metrics.completed_cases}")
    for gate in gates:
        observed = "not measured" if gate.observed is None else f"{gate.observed:,.4f}"
        print(f"  {gate.status.value.upper():<10} {gate.metric} {gate.bound} (observed {observed})")
    print(f"  report: {json_path}")
    print(f"  summary: {markdown_path}")

    if status is GateStatus.PASS:
        return EXIT_PASS
    return EXIT_FAIL if status is GateStatus.FAIL else EXIT_INCOMPLETE


def compare_command(arguments: argparse.Namespace) -> int:
    """Diff two reports. Exit 1 when the candidate regressed."""
    try:
        policy = load_policy()
        baseline = load_report(Path(arguments.baseline))
        candidate = load_report(Path(arguments.candidate))
    except AssistantError as error:
        print(f"Comparison could not start: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION

    comparison = compare_reports(baseline, candidate, policy.regression)
    print(render_comparison(comparison))
    return EXIT_FAIL if comparison.has_regression else EXIT_PASS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform_engineering_assistant.evaluation.generation_cli",
        description=(
            "Generation and end-to-end evaluation for the platform engineering "
            "assistant. Live evaluation is manual and calls a metered deployment."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Evaluate the dataset and write a report.")
    run.add_argument(
        "--mode",
        choices=[mode.value for mode in ExecutionMode],
        default=ExecutionMode.FAKE.value,
        help="fake: deterministic, offline, free. live: calls the real deployment.",
    )
    run.add_argument(
        "--judge",
        action="store_true",
        help="Enable the structured semantic judge. Requires --mode live and doubles the calls.",
    )
    run.add_argument(
        "--delay-seconds",
        type=float,
        default=None,
        help="Pacing between cases. Defaults to 0 offline and 2 seconds live.",
    )
    run.add_argument("--output", default=None, help="Directory for the reports.")
    run.set_defaults(handler=run_command)

    compare = subparsers.add_parser("compare", help="Diff two evaluation reports.")
    compare.add_argument("baseline", help="Path to the accepted baseline report JSON.")
    compare.add_argument("candidate", help="Path to the candidate report JSON.")
    compare.set_defaults(handler=compare_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    handler = arguments.handler
    result: int = handler(arguments)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
