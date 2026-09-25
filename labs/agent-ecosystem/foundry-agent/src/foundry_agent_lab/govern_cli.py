"""The live command for the governed (19.1c) path. Manual, never automatic.

Runs the four proofs in ONE process, in order, printing each stage:

    1. Foundry proposes the action
    2. policy blocks execution and records an approval
    3. a human decision is recorded through the Phase 18 approval service
    4. the approved workflow resumes exactly once

They share a process deliberately. The approval store and the workflow store are
both in-memory — the same limitation Phase 18 carries and records as open — so
splitting the stages across four shell invocations would lose the state between
them. Making the stores durable is production-hardening work, not something to
fake here with a temporary file that hides the gap.
"""

from __future__ import annotations

import argparse
import json
import sys

from foundry_agent_lab.config import LabConfigurationError, load_config
from foundry_agent_lab.docs_search import _product_search_tool
from foundry_agent_lab.governance import GovernedToolGateway
from foundry_agent_lab.governed_runtime import GovernedFoundryLoop, turn_as_json
from foundry_agent_lab.protocol import AgentRuntime, ProposedCall, RuntimeResponse
from foundry_agent_lab.resume import ApprovedActionRunner
from foundry_agent_lab.workflow import InMemoryWorkflowStore

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIGURATION = 3


def _offline_runtime(question: str) -> AgentRuntime:
    """A scripted runtime that proposes the state-changing tool."""
    from foundry_agent_lab.protocol import FakeAgentRuntime

    return FakeAgentRuntime(
        conversation_id="conv-offline",
        scripted=[
            RuntimeResponse(
                response_id="resp-offline-1",
                conversation_id="conv-offline",
                proposed_calls=(
                    ProposedCall(
                        call_id="call-1",
                        name="propose_change_request",
                        arguments={
                            "title": "Raise sandbox Foundry capacity to 30",
                            "rationale": "Evaluation runs are being throttled at capacity 10.",
                        },
                    ),
                ),
            ),
            RuntimeResponse(
                response_id="resp-offline-2",
                conversation_id="conv-offline",
                output_text=(
                    "(offline fake) The change request was recorded and is awaiting "
                    "human approval. It has not been carried out."
                ),
                model="fake-deterministic",
                total_tokens=0,
            ),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="foundry-agent-govern",
        description="Run one governed turn, then approve and resume it.",
    )
    parser.add_argument("--question", required=True)
    parser.add_argument("--offline", action="store_true", help="Use the deterministic fake.")
    parser.add_argument(
        "--approve-as",
        default=None,
        help="Record a human approval as this principal, then resume. Omit to stop at stage 2.",
    )
    arguments = parser.parse_args(argv)

    gateway = GovernedToolGateway(_product_search_tool()._index)
    store = InMemoryWorkflowStore()

    if arguments.offline:
        runtime: AgentRuntime = _offline_runtime(arguments.question)
    else:
        try:
            config = load_config()
        except LabConfigurationError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return EXIT_CONFIGURATION
        from foundry_agent_lab.foundry import FoundryAgentRuntime

        runtime = FoundryAgentRuntime.from_config(config)

    # Provisioning first, as its own step: serving a turn never writes to the
    # agent definition. See provisioning.py.
    from foundry_agent_lab.governed_runtime import GOVERNED_INSTRUCTIONS
    from foundry_agent_lab.provisioning import provision_agent

    agent_name = "offline-fake" if arguments.offline else load_config().agent_name
    loop = GovernedFoundryLoop(runtime, gateway, store)
    provisioned = provision_agent(runtime, agent_name, loop.tool_schemas(), GOVERNED_INSTRUCTIONS)
    print("=== 0. provisioning (separate from serving) ===")
    print(json.dumps(provisioned.as_dict(), indent=2))

    print("\n=== 1+2. Foundry proposes; the application rules ===")
    loop = GovernedFoundryLoop(
        runtime,
        gateway,
        store,
        agent_name=provisioned.agent_name,
        agent_version=provisioned.agent_version,
    )
    turn = loop.run(arguments.question)
    print(turn_as_json(turn))

    if not turn.approval_required:
        print("\nNo state-changing action was proposed; nothing to approve.")
        return EXIT_OK if turn.final_text else EXIT_FAILED

    approval_id = turn.approval_ids[0]
    workflow_id = turn.workflow_ids[0]
    record = gateway.approvals.store.get(approval_id)
    print("\n=== 2. the recorded approval (nothing executed) ===")
    print(
        json.dumps(
            {
                "approval_id": record.approval_id,
                "status": record.status.value,
                "effective_status": record.effective_status().value,
                "tool_name": record.action.tool_name,
                "argument_fingerprint": record.action.argument_fingerprint,
                "summary": record.action.summary,
                "requested_by": record.requested_by,
                "expires_at": record.expires_at,
            },
            indent=2,
        )
    )

    if not arguments.approve_as:
        print("\nStopping before approval. Re-run with --approve-as <human> to continue.")
        return EXIT_OK

    print("\n=== 3. human approval ===")
    from platform_engineering_assistant.agent.approval import ApprovalError

    try:
        decided = gateway.approvals.decide(
            approval_id, approved=True, approver=arguments.approve_as
        )
    except ApprovalError as error:
        print(f"Approval refused: {error.reason} ({error})")
        return EXIT_FAILED
    print(f"status={decided.status.value} decided_by={decided.decided_by} reason={decided.reason}")

    print("\n=== 4. controlled resume (at most once) ===")
    runner = ApprovedActionRunner(gateway, store)
    first = runner.resume(workflow_id)
    second = runner.resume(workflow_id)
    print(
        json.dumps(
            {
                "first_resume": {
                    "state": first.state.value,
                    "executed": first.executed,
                    "tool_call_id": first.tool_call_id,
                    "reason": first.reason,
                },
                "second_resume": {
                    "state": second.state.value,
                    "executed": second.executed,
                    "reason": second.reason,
                },
                "executed_at_most_once": not (first.executed and second.executed),
            },
            indent=2,
        )
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
