"""Command line entry point for the agent evaluation.

    uv run python -m platform_engineering_assistant.evaluation.agent_cli run --mode fake

EXIT CODES ARE MEANINGFUL, AND INCOMPLETE IS NOT SUCCESS
---------------------------------------------------------
    0  every applicable gate passed
    1  a gate failed
    2  the run was incomplete — a required metric could not be computed, or the
       run aborted part way. "We did not measure this" must not be
       indistinguishable from "this passed".
    3  misconfiguration: a dataset, policy or corpus that could not be loaded.

LIVE IS NEVER THE DEFAULT AND NEVER AUTOMATIC
----------------------------------------------
`--mode live` calls a metered deployment twice per tool-using case. It stays a
deliberate, human-invoked command; CI runs fake mode only, with no credential
available to it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from platform_engineering_assistant.agent.protocol import (
    AgentDecisionProvider,
    FakeAgentDecisionProvider,
)
from platform_engineering_assistant.answering import build_service
from platform_engineering_assistant.config import PRODUCT_ROOT
from platform_engineering_assistant.errors import AssistantError
from platform_engineering_assistant.evaluation.agent_dataset import load_agent_dataset
from platform_engineering_assistant.evaluation.agent_metrics import compute_agent_metrics
from platform_engineering_assistant.evaluation.agent_report import (
    DEFAULT_AGENT_ARTIFACTS_DIR,
    AgentReportInputs,
    AgentRunProvenance,
    write_agent_reports,
)
from platform_engineering_assistant.evaluation.agent_runner import (
    build_evaluation_agent,
    run_agent_dataset,
)
from platform_engineering_assistant.evaluation.policy import (
    ExecutionMode,
    GateStatus,
    evaluate_gates,
    load_policy,
    overall_status,
)
from platform_engineering_assistant.evaluation.provenance import (
    git_sha,
    utc_now_iso,
    working_tree_is_dirty,
)
from platform_engineering_assistant.generation.protocol import GenerationProvider

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCOMPLETE = 2
EXIT_CONFIGURATION = 3

AGENT_POLICY_PATH = PRODUCT_ROOT / "evaluation" / "agent_evaluation_policy_v1.json"

# A live run against a capacity-1 deployment, where a tool-using case costs two
# model calls. Paced more slowly than the answering suite for that reason.
DEFAULT_LIVE_DELAY_SECONDS = 3.0


def _build_providers(
    mode: ExecutionMode,
) -> tuple[GenerationProvider, AgentDecisionProvider, str | None]:
    """Construct the answering and decision providers for the mode.

    The Azure imports live inside the live branch so the fake path — the one CI
    takes — imports no Azure SDK at all and cannot accidentally acquire a
    credential.
    """
    if mode is ExecutionMode.FAKE:
        from platform_engineering_assistant.generation.fake import FakeGenerationProvider

        return FakeGenerationProvider(), FakeAgentDecisionProvider(), None

    from platform_engineering_assistant.agent.protocol import (
        AzureOpenAIAgentDecisionProvider,
    )
    from platform_engineering_assistant.generation.azure_openai import (
        AzureOpenAIGenerationProvider,
    )
    from platform_engineering_assistant.provider_config import load_provider_config

    config = load_provider_config()
    return (
        AzureOpenAIGenerationProvider.from_config(config),
        AzureOpenAIAgentDecisionProvider.from_config(config),
        config.deployment,
    )


def run_command(arguments: argparse.Namespace) -> int:
    """Evaluate the agent dataset and write a report."""
    mode = ExecutionMode(arguments.mode)

    try:
        dataset = load_agent_dataset()
        policy = load_policy(AGENT_POLICY_PATH)
        provider, decider, deployment = _build_providers(mode)
        answering = build_service(provider)
        agent = build_evaluation_agent(answering, decider)
    except AssistantError as error:
        print(f"Agent evaluation could not start: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION

    delay = (
        arguments.delay_seconds
        if arguments.delay_seconds is not None
        else (DEFAULT_LIVE_DELAY_SECONDS if mode is ExecutionMode.LIVE else 0.0)
    )

    result = run_agent_dataset(
        agent,
        dataset,
        mode=mode,
        decider=decider if mode is ExecutionMode.LIVE else None,
        inter_case_delay_seconds=delay,
    )
    metrics = compute_agent_metrics(result.outcomes, result.planned_cases)

    gates = evaluate_gates(policy, metrics.as_observed(), mode, judge_enabled=False)
    applicable = set(policy.applicable(mode, judge_enabled=False))
    unassessed = tuple(sorted(set(policy.thresholds) - applicable))

    incomplete_reasons: list[str] = []
    if not result.operationally_complete:
        incomplete_reasons.append(
            f"the run did not complete: {result.aborted_on or 'unknown reason'}"
        )

    provenance = AgentRunProvenance(
        git_sha=git_sha(),
        git_dirty=working_tree_is_dirty(),
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset.content_sha256,
        corpus_version=answering.corpus_version,
        agent_prompt_version=agent.agent_prompt.version,
        agent_prompt_sha256=agent.agent_prompt.content_hash,
        prompt_version=answering.prompt.version,
        retrieval_config_version=answering.retrieval_config.version,
        provider=provider.name,
        deployment=result.observed_deployment or deployment,
        model=result.observed_model,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_sha256=policy.content_sha256,
        generated_at=utc_now_iso(),
        case_count=len(dataset.cases),
        execution_mode=mode.value,
    )

    if provenance.unattributable_live_run:
        incomplete_reasons.append("a live run from a dirty tree cannot be attributed to a commit")

    status = overall_status(
        gates,
        extra_incomplete=bool(incomplete_reasons),
    )

    inputs = AgentReportInputs(
        provenance=provenance,
        metrics=metrics,
        gates=gates,
        outcomes=result.outcomes,
        status=status,
        operationally_complete=result.operationally_complete,
        aborted_on=result.aborted_on.value if result.aborted_on else None,
        dataset_limitations=dataset.limitations,
        unassessed_metrics=unassessed,
        incomplete_reasons=tuple(incomplete_reasons),
    )

    directory = Path(arguments.output) if arguments.output else DEFAULT_AGENT_ARTIFACTS_DIR
    json_path, markdown_path = write_agent_reports(inputs, directory)
    print(f"Agent evaluation report written to {json_path} and {markdown_path}")

    if status is GateStatus.FAIL:
        return EXIT_FAIL
    if status is GateStatus.INCOMPLETE:
        return EXIT_INCOMPLETE
    return EXIT_PASS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-evaluation",
        description="Evaluate the controlled agent against its versioned case set.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Evaluate the agent dataset and write a report.")
    run.add_argument(
        "--mode",
        choices=[mode.value for mode in ExecutionMode],
        default=ExecutionMode.FAKE.value,
        help="fake uses the deterministic provider and each case's scripted proposal.",
    )
    run.add_argument("--output", default=None, help="Directory for the report artefacts.")
    run.add_argument(
        "--delay-seconds",
        type=float,
        default=None,
        help="Pace between cases. Changes spacing only; never how many calls are made.",
    )
    run.set_defaults(handler=run_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    handler = arguments.handler
    result: int = handler(arguments)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
