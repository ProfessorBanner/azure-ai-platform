"""The evaluation runner: cases in, correlated outcomes out.

ONE GOVERNED PATH
-----------------
The runner drives `GovernedFoundryLoop` — the same class a live turn uses, over
the same product registry, policy, approval service and executor. There is no
evaluation-only governance. A parallel path would measure the parallel path.

WHAT DIFFERS BETWEEN MODES
--------------------------
In FAKE mode the case's scripted calls are injected as the managed runtime's
proposal. In LIVE mode a real Foundry agent proposes and the scripted calls are
ignored. Everything downstream is identical, which is the whole point: the
controls under test are downstream of the only thing being swapped.
"""

from __future__ import annotations

from dataclasses import dataclass

from foundry_agent_lab.correlation import correlate, verify
from foundry_agent_lab.evaluation.dataset import LabCase, LabDataset
from foundry_agent_lab.evaluation.metrics import LabCaseOutcome
from foundry_agent_lab.governance import GovernedToolGateway
from foundry_agent_lab.governed_runtime import GovernedFoundryLoop
from foundry_agent_lab.protocol import (
    AgentRuntime,
    FakeAgentRuntime,
    ProposedCall,
    RuntimeResponse,
)
from foundry_agent_lab.workflow import InMemoryWorkflowStore

FINAL_TEXT = "(evaluation fake) responded from the tool outputs."


def scripted_runtime(case: LabCase) -> AgentRuntime:
    """A runtime that proposes exactly what the case scripts, then answers."""
    responses: list[RuntimeResponse] = []
    if case.scripted_calls:
        responses.append(
            RuntimeResponse(
                response_id=f"resp-{case.case_id}-1",
                conversation_id=f"conv-{case.case_id}",
                proposed_calls=tuple(
                    ProposedCall(
                        call_id=f"call-{index}", name=call.name, arguments=dict(call.arguments)
                    )
                    for index, call in enumerate(case.scripted_calls, start=1)
                ),
            )
        )
    responses.append(
        RuntimeResponse(
            response_id=f"resp-{case.case_id}-final",
            conversation_id=f"conv-{case.case_id}",
            output_text=FINAL_TEXT,
            model="fake-deterministic",
            total_tokens=0,
        )
    )
    return FakeAgentRuntime(scripted=responses, conversation_id=f"conv-{case.case_id}")


@dataclass(frozen=True, slots=True)
class LabRunResult:
    outcomes: list[LabCaseOutcome]
    planned_cases: int

    @property
    def complete(self) -> bool:
        return len(self.outcomes) == self.planned_cases


def _outcome_for(
    case: LabCase, gateway: GovernedToolGateway, runtime: AgentRuntime
) -> LabCaseOutcome:
    store = InMemoryWorkflowStore()
    loop = GovernedFoundryLoop(
        runtime, gateway, store, agent_name="eval-agent", agent_version="eval-1"
    )
    try:
        turn = loop.run(case.question)
    except Exception as error:  # noqa: BLE001 - an escape IS the finding
        return LabCaseOutcome(
            case_id=case.case_id,
            category=case.category,
            expected_outcome=case.expected_outcome,
            expected_policy_decision=case.expected_policy_decision,
            expected_tool=case.expected_tool,
            expects_tool_use=case.expects_tool_use,
            expects_execution=case.expects_execution,
            error_class=type(error).__name__,
        )

    record = correlate(turn, case.question)
    defects = verify(record)
    first = turn.results[0] if turn.results else None
    executed = bool(turn.executed_tools)

    return LabCaseOutcome(
        case_id=case.case_id,
        category=case.category,
        expected_outcome=case.expected_outcome,
        observed_outcome=record.outcome,
        expected_policy_decision=case.expected_policy_decision,
        observed_policy_decision=first.policy_decision if first else None,
        expected_tool=case.expected_tool,
        observed_tool=first.tool_name if first else None,
        expects_tool_use=case.expects_tool_use,
        expects_execution=case.expects_execution,
        executed=executed,
        approval_id=turn.approval_ids[0] if turn.approval_ids else None,
        argument_fingerprint=first.argument_fingerprint if first else None,
        # Read from the correlation record, by the same rule verify() applies:
        # a response can only say what the application believes it did.
        unauthorised_execution=any(
            event.outcome == "executed" and event.risk != "read_only"
            for event in record.tool_events
        ),
        trajectory_defects=defects,
        correlated=bool(record.response_ids),
        question_chars=record.question_chars,
        total_tokens=record.total_tokens,
    )


def run_dataset(
    dataset: LabDataset,
    gateway: GovernedToolGateway,
    runtime_for: object | None = None,
) -> LabRunResult:
    """Run every case once through the governed loop.

    `runtime_for` may supply a live runtime factory; when absent, each case's
    scripted proposal is injected.
    """
    outcomes: list[LabCaseOutcome] = []
    for case in dataset.cases:
        runtime = (
            runtime_for(case)  # type: ignore[operator]
            if runtime_for is not None
            else scripted_runtime(case)
        )
        outcomes.append(_outcome_for(case, gateway, runtime))
    return LabRunResult(outcomes=outcomes, planned_cases=len(dataset.cases))


__all__ = ["FINAL_TEXT", "LabRunResult", "run_dataset", "scripted_runtime"]
