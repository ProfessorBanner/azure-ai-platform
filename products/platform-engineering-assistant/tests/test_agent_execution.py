"""Phase 18.2: typed failures, timeouts, bounded retry and idempotency."""

from __future__ import annotations

import time
from typing import Any

import pytest

from platform_engineering_assistant.agent.domain import ToolRiskLevel
from platform_engineering_assistant.agent.execution import (
    RETRYABLE_CATEGORIES,
    ExecutionStatus,
    InMemoryIdempotencyStore,
    InvalidToolInputError,
    PermanentToolError,
    PolicyDeniedError,
    RetryPolicy,
    ToolExecutor,
    ToolFailureCategory,
    ToolTimeoutError,
    TransientToolError,
    category_of,
    idempotency_key_for,
    run_with_timeout,
)
from platform_engineering_assistant.agent.registry import ToolDefinition
from platform_engineering_assistant.agent.tools import (
    ProposeChangeInput,
    SearchDocsInput,
    SearchDocsOutput,
    Tool,
)


class ScriptedTool(Tool[SearchDocsInput, SearchDocsOutput]):
    """A tool that fails according to a script, then succeeds."""

    name = "scripted"
    description = "test double"
    risk = ToolRiskLevel.READ_ONLY
    input_model = SearchDocsInput
    output_model = SearchDocsOutput

    def __init__(self, failures: list[Exception], *, delay: float = 0.0) -> None:
        self._failures = list(failures)
        self._delay = delay
        self.calls = 0

    def run(self, payload: SearchDocsInput) -> SearchDocsOutput:
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        if self._failures:
            raise self._failures.pop(0)
        return SearchDocsOutput(result_count=1)


def definition_for(tool: Any, risk: ToolRiskLevel | None = None) -> ToolDefinition:
    """`Tool` is invariant in its type parameters, so test doubles are passed as
    `Any` rather than scattering an ignore at every call site."""
    return ToolDefinition(
        name=tool.name,
        description=tool.description,
        risk=risk or tool.risk,
        tool=tool,
    )


PAYLOAD = SearchDocsInput(query="terraform state")
NO_WAIT = ToolExecutor(sleeper=lambda _: None)


def executor(policy: RetryPolicy | None = None, **kwargs: object) -> ToolExecutor:
    return ToolExecutor(
        retry_policy=policy or RetryPolicy(max_attempts=3, base_delay_seconds=0.0),
        sleeper=lambda _: None,
        **kwargs,  # type: ignore[arg-type]
    )


# --- classification ----------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ToolTimeoutError("t"), ToolFailureCategory.TIMEOUT),
        (TransientToolError("t"), ToolFailureCategory.TRANSIENT_FAILURE),
        (PermanentToolError("t"), ToolFailureCategory.PERMANENT_FAILURE),
        (InvalidToolInputError("t"), ToolFailureCategory.INVALID_INPUT),
        (PolicyDeniedError("t"), ToolFailureCategory.POLICY_DENIED),
    ],
)
def test_every_typed_failure_classifies_to_its_category(
    error: Exception, expected: ToolFailureCategory
) -> None:
    assert category_of(error) is expected


def test_an_unclassified_failure_defaults_to_permanent() -> None:
    """Failing towards 'do not retry': nobody has reasoned about this error."""
    assert category_of(ValueError("who knows")) is ToolFailureCategory.PERMANENT_FAILURE


def test_only_timeout_and_transient_are_retryable() -> None:
    assert RETRYABLE_CATEGORIES == {
        ToolFailureCategory.TIMEOUT,
        ToolFailureCategory.TRANSIENT_FAILURE,
    }


# --- timeout -----------------------------------------------------------------


def test_a_slow_tool_times_out_for_real() -> None:
    """Exercises the real ThreadPoolExecutor mechanism, not a stub."""
    with pytest.raises(ToolTimeoutError):
        run_with_timeout(lambda: time.sleep(0.5) or SearchDocsOutput(), 0.02)  # type: ignore[func-returns-value]


def test_a_fast_tool_returns_its_output() -> None:
    output = run_with_timeout(lambda: SearchDocsOutput(result_count=3), 5.0)
    assert isinstance(output, SearchDocsOutput)
    assert output.result_count == 3


def test_a_timeout_is_recorded_with_its_category() -> None:
    tool = ScriptedTool([ToolTimeoutError("slow")] * 3)
    outcome = executor().execute(definition_for(tool), PAYLOAD)
    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.failure_category is ToolFailureCategory.TIMEOUT


def test_per_tool_timeouts_override_the_default() -> None:
    runner = ToolExecutor(default_timeout_seconds=10.0, timeouts={"scripted": 0.5})
    assert runner.timeout_for("scripted") == 0.5
    assert runner.timeout_for("something_else") == 10.0


# --- retry -------------------------------------------------------------------


def test_a_transient_failure_is_retried_and_can_succeed() -> None:
    tool = ScriptedTool([TransientToolError("blip")])
    outcome = executor().execute(definition_for(tool), PAYLOAD)

    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert tool.calls == 2
    assert outcome.attempts == 2
    assert outcome.records[0].status is ExecutionStatus.FAILED
    assert outcome.records[1].status is ExecutionStatus.SUCCEEDED


def test_retries_are_exhausted_and_then_reported() -> None:
    tool = ScriptedTool([TransientToolError("blip")] * 5)
    outcome = executor(RetryPolicy(max_attempts=3, base_delay_seconds=0.0)).execute(
        definition_for(tool),
        PAYLOAD,
    )
    assert outcome.status is ExecutionStatus.FAILED
    assert tool.calls == 3
    assert outcome.attempts == 3
    assert outcome.failure_category is ToolFailureCategory.TRANSIENT_FAILURE


@pytest.mark.parametrize(
    "error",
    [PermanentToolError("no"), InvalidToolInputError("no"), PolicyDeniedError("no")],
)
def test_a_non_retryable_failure_is_attempted_exactly_once(error: Exception) -> None:
    """Retrying invalid input turns a bug into a storm; retrying a policy denial
    is an attempt to get a different answer from a deterministic function."""
    tool = ScriptedTool([error] * 5)
    outcome = executor().execute(definition_for(tool), PAYLOAD)
    assert tool.calls == 1
    assert outcome.attempts == 1
    assert outcome.status is ExecutionStatus.FAILED


def test_max_attempts_is_enforced() -> None:
    for budget in (1, 2, 5):
        tool = ScriptedTool([TransientToolError("blip")] * 10)
        executor(RetryPolicy(max_attempts=budget, base_delay_seconds=0.0)).execute(
            definition_for(tool),
            PAYLOAD,
        )
        assert tool.calls == budget


def test_backoff_is_exponential_and_bounded() -> None:
    policy = RetryPolicy(
        max_attempts=6, base_delay_seconds=0.1, max_delay_seconds=0.5, multiplier=2.0
    )
    delays = [policy.delay_before(attempt) for attempt in range(1, 7)]
    assert delays[0] == 0.0  # nothing to back off from before the first attempt
    assert delays[1:4] == [0.1, 0.2, 0.4]
    assert all(delay <= 0.5 for delay in delays)


def test_a_retry_policy_must_be_coherent() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="multiplier"):
        RetryPolicy(multiplier=0.5)
    with pytest.raises(ValueError, match="negative"):
        RetryPolicy(base_delay_seconds=-1.0)


def test_the_executor_sleeps_between_attempts() -> None:
    slept: list[float] = []
    tool = ScriptedTool([TransientToolError("blip")] * 2)
    ToolExecutor(
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.1),
        sleeper=slept.append,
    ).execute(definition_for(tool), PAYLOAD)
    assert slept == [0.1, 0.2]


# --- idempotency --------------------------------------------------------------


class RecordingProposal(Tool[ProposeChangeInput, SearchDocsOutput]):
    name = "propose_change_request"
    description = "test double for a consequential action"
    risk = ToolRiskLevel.STATE_CHANGING
    input_model = ProposeChangeInput
    output_model = SearchDocsOutput

    def __init__(self) -> None:
        self.performed: list[str] = []

    def run(self, payload: ProposeChangeInput) -> SearchDocsOutput:
        self.performed.append(payload.title)
        return SearchDocsOutput(result_count=1)


PROPOSAL = ProposeChangeInput(title="Raise capacity", rationale="Throttled during evaluation")


def test_a_state_changing_tool_is_denied_without_approval() -> None:
    tool = RecordingProposal()
    outcome = executor().execute(definition_for(tool), PROPOSAL)

    assert outcome.status is ExecutionStatus.DENIED
    assert outcome.failure_category is ToolFailureCategory.POLICY_DENIED
    assert tool.performed == []


def test_an_approved_state_changing_tool_executes_once() -> None:
    tool = RecordingProposal()
    outcome = executor().execute(definition_for(tool), PROPOSAL, approved=True)

    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert tool.performed == ["Raise capacity"]
    assert outcome.idempotency_key


def test_a_repeated_action_cannot_execute_twice() -> None:
    """The whole point: a repeated request must not produce a second action."""
    tool = RecordingProposal()
    runner = executor()

    first = runner.execute(definition_for(tool), PROPOSAL, approved=True)
    second = runner.execute(definition_for(tool), PROPOSAL, approved=True)

    assert first.status is ExecutionStatus.SUCCEEDED
    assert second.status is ExecutionStatus.DUPLICATE_BLOCKED
    assert tool.performed == ["Raise capacity"]


def test_a_blocked_duplicate_returns_the_original_outcome() -> None:
    tool = RecordingProposal()
    runner = executor()
    runner.execute(definition_for(tool), PROPOSAL, approved=True)
    second = runner.execute(definition_for(tool), PROPOSAL, approved=True)
    assert second.output is not None


def test_different_arguments_are_different_actions() -> None:
    tool = RecordingProposal()
    runner = executor()
    other = ProposeChangeInput(title="Different change", rationale="A different rationale here")

    runner.execute(definition_for(tool), PROPOSAL, approved=True)
    runner.execute(definition_for(tool), other, approved=True)

    assert tool.performed == ["Raise capacity", "Different change"]


def test_the_idempotency_key_is_derived_not_supplied() -> None:
    """A model that chose its own key could defeat duplicate detection by
    choosing a fresh one."""
    first = idempotency_key_for("propose_change_request", PROPOSAL)
    same = idempotency_key_for(
        "propose_change_request",
        ProposeChangeInput(title="Raise capacity", rationale="Throttled during evaluation"),
    )
    assert first == same
    assert len(first) == 64


def test_the_key_is_independent_of_field_ordering() -> None:
    a = ProposeChangeInput(title="Raise capacity", rationale="A rationale that is long enough")
    b = ProposeChangeInput(rationale="A rationale that is long enough", title="Raise capacity")
    assert idempotency_key_for("t", a) == idempotency_key_for("t", b)


def test_the_key_differs_per_tool() -> None:
    assert idempotency_key_for("tool_a", PROPOSAL) != idempotency_key_for("tool_b", PROPOSAL)


def test_read_only_tools_carry_no_idempotency_key() -> None:
    """Reads are naturally idempotent; a key would only add contention."""
    tool = ScriptedTool([])
    outcome = executor().execute(definition_for(tool), PAYLOAD)
    assert outcome.idempotency_key is None


def test_a_read_only_tool_may_run_repeatedly() -> None:
    tool = ScriptedTool([])
    runner = executor()
    runner.execute(definition_for(tool), PAYLOAD)
    runner.execute(definition_for(tool), PAYLOAD)
    assert tool.calls == 2


def test_a_failed_action_keeps_its_claim() -> None:
    """Whether repeating a failed consequential action is safe is a human's
    decision, not something to infer from an exception type."""
    tool = RecordingProposal()

    def always_fails(payload: ProposeChangeInput) -> SearchDocsOutput:
        raise PermanentToolError("backend refused")

    tool.run = always_fails  # type: ignore[method-assign]
    runner = executor()

    first = runner.execute(definition_for(tool), PROPOSAL, approved=True)
    second = runner.execute(definition_for(tool), PROPOSAL, approved=True)

    assert first.status is ExecutionStatus.FAILED
    assert second.status is ExecutionStatus.DUPLICATE_BLOCKED


def test_the_store_claims_atomically() -> None:
    store = InMemoryIdempotencyStore()
    assert store.claim("k") is True
    assert store.claim("k") is False
    store.release("k")
    assert store.claim("k") is True


# --- execution records --------------------------------------------------------


def test_every_attempt_produces_a_record() -> None:
    tool = ScriptedTool([TransientToolError("blip"), TransientToolError("blip")])
    outcome = executor().execute(definition_for(tool), PAYLOAD)

    assert [record.attempt for record in outcome.records] == [1, 2, 3]
    for record in outcome.records:
        assert record.tool_call_id == outcome.tool_call_id
        assert record.tool_name == "scripted"
        assert record.started_at and record.completed_at
        assert record.latency_ms >= 0.0


def test_records_carry_no_arguments_or_output() -> None:
    """The audit unit is identifiers and timings, never the payload."""
    import dataclasses

    from platform_engineering_assistant.agent.execution import ToolExecutionRecord

    names = {field.name for field in dataclasses.fields(ToolExecutionRecord)}
    for forbidden in ("payload", "arguments", "output", "result", "query"):
        assert forbidden not in names


def test_the_tool_call_id_is_stable_across_attempts() -> None:
    tool = ScriptedTool([TransientToolError("blip")])
    outcome = executor().execute(definition_for(tool), PAYLOAD)
    assert len({record.tool_call_id for record in outcome.records}) == 1
