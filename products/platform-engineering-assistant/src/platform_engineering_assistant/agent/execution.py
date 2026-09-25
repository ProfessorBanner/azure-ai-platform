"""Tool execution reliability: typed failures, timeouts, bounded retry, idempotency.

THE SPLIT THIS MODULE PRESERVES
-------------------------------
    model      proposes
    policy     authorises
    executor   executes

The executor never decides whether a call is permitted. It receives an already
authorised definition and payload, and its only concerns are: did it finish in
time, is the failure worth retrying, and has this action already happened.

RETRY IS A CLASSIFICATION PROBLEM, NOT A LOOP
---------------------------------------------
Retrying is only correct when a failure is genuinely transient. Retrying a
permanent failure turns one error into several; retrying invalid input turns a
bug into a storm; retrying a policy denial is an attempt to get a different
answer from a deterministic function. So the retry decision is driven entirely
by a TYPED failure category, and the default for an unrecognised failure is not
to retry.

IDEMPOTENCY IS FOR ACTIONS, NOT FOR READS
-----------------------------------------
Read-only tools are naturally idempotent: running a search twice costs time and
nothing else. State-changing tools are not, and a retry that duplicated a
consequential action would be worse than the failure it was recovering from.
Every state-changing execution therefore claims an idempotency key derived from
its own validated arguments, and a key that has already been claimed cannot be
claimed again.

WHAT THE TIMEOUT DOES AND DOES NOT DO
-------------------------------------
`run_with_timeout` returns control to the caller at the deadline. It does NOT
kill the worker: Python cannot safely terminate a running thread, and pretending
otherwise would be the more dangerous lie. An abandoned worker finishes in the
background and its result is discarded. That is acceptable for the read-only,
in-process tools this product has, and it is recorded here so that a future tool
performing real I/O is designed with cancellation rather than assuming it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from platform_engineering_assistant.agent.domain import ToolRiskLevel
from platform_engineering_assistant.agent.registry import ToolDefinition
from platform_engineering_assistant.agent.tools import ToolError, ToolInput, ToolOutput

DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 0.2
DEFAULT_MAX_DELAY_SECONDS = 2.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0


class ToolFailureCategory(StrEnum):
    """Why a tool execution did not succeed. Drives the retry decision."""

    TIMEOUT = "timeout"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"
    INVALID_INPUT = "invalid_input"
    POLICY_DENIED = "policy_denied"
    # A tool returned something that is not its declared output type. Never
    # retryable: a tool that answers with the wrong shape will answer with the
    # wrong shape again, and the retry would only delay the failure.
    MALFORMED_OUTPUT = "malformed_output"


# The ONLY categories that may be retried. Written as an explicit allow-list so
# that adding a category defaults to not-retryable rather than silently
# inheriting retry behaviour it was never assessed for.
RETRYABLE_CATEGORIES = frozenset(
    {ToolFailureCategory.TIMEOUT, ToolFailureCategory.TRANSIENT_FAILURE}
)


class ToolTimeoutError(ToolError):
    """The tool did not complete within its configured timeout."""

    failure_category = ToolFailureCategory.TIMEOUT


class TransientToolError(ToolError):
    """A failure that may succeed on a later attempt."""

    failure_category = ToolFailureCategory.TRANSIENT_FAILURE


class PermanentToolError(ToolError):
    """A failure that will recur identically. Never retried."""

    failure_category = ToolFailureCategory.PERMANENT_FAILURE


class InvalidToolInputError(ToolError):
    """Arguments did not satisfy the tool's contract. Never retried."""

    failure_category = ToolFailureCategory.INVALID_INPUT


class PolicyDeniedError(ToolError):
    """Execution was refused by policy. Never retried.

    Retrying a deterministic authorisation function is an attempt to get a
    different answer from the same inputs.
    """

    failure_category = ToolFailureCategory.POLICY_DENIED


def category_of(error: BaseException) -> ToolFailureCategory:
    """Classify any exception. Unrecognised failures are PERMANENT.

    Failing towards "do not retry" is deliberate: an unclassified error is one
    nobody has reasoned about, and hammering it is the wrong default.
    """
    declared = getattr(error, "failure_category", None)
    if isinstance(declared, ToolFailureCategory):
        return declared
    if isinstance(error, TimeoutError | FuturesTimeoutError):
        return ToolFailureCategory.TIMEOUT
    return ToolFailureCategory.PERMANENT_FAILURE


class ExecutionStatus(StrEnum):
    """The outcome of one execution attempt, or of the whole call."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    DUPLICATE_BLOCKED = "duplicate_blocked"


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    """One attempt at one tool call. The audit unit of the executor.

    Carries no arguments and no output: the idempotency key is a hash, the
    payload is not reproduced, and a caller wanting the result gets the typed
    output object rather than a serialised copy in the audit trail.
    """

    tool_call_id: str
    tool_name: str
    attempt: int
    status: ExecutionStatus
    started_at: str
    completed_at: str
    latency_ms: float
    failure_category: ToolFailureCategory | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Everything one `execute` call produced, across all its attempts."""

    tool_call_id: str
    tool_name: str
    status: ExecutionStatus
    output: ToolOutput | None
    records: tuple[ToolExecutionRecord, ...]
    failure_category: ToolFailureCategory | None = None
    idempotency_key: str | None = None

    @property
    def attempts(self) -> int:
        return len(self.records)

    @property
    def total_latency_ms(self) -> float:
        return sum(record.latency_ms for record in self.records)

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """A bounded backoff policy. Every field is a ceiling, not a target."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS
    multiplier: float = DEFAULT_BACKOFF_MULTIPLIER

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delays must not be negative")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be at least 1.0")

    def should_retry(self, category: ToolFailureCategory, attempt: int) -> bool:
        """Retry only an explicitly retryable category, within the attempt budget."""
        if attempt >= self.max_attempts:
            return False
        return category in RETRYABLE_CATEGORIES

    def delay_before(self, attempt: int) -> float:
        """Backoff before `attempt` (1-based). Bounded by `max_delay_seconds`.

        No jitter. A single-caller evaluation and support tool has no thundering
        herd to spread, and deterministic delays keep the tests honest.
        """
        if attempt <= 1:
            return 0.0
        delay = self.base_delay_seconds * (self.multiplier ** (attempt - 2))
        return min(delay, self.max_delay_seconds)


def idempotency_key_for(tool_name: str, payload: ToolInput) -> str:
    """A stable key derived from the tool and its VALIDATED arguments.

    Derived rather than supplied by the model on purpose: a model that chose its
    own key could defeat duplicate detection by choosing a fresh one, which is
    exactly the failure the key exists to prevent. Canonical JSON with sorted
    keys makes the key independent of field ordering.
    """
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{tool_name}\x00{canonical}".encode()).hexdigest()
    return digest


class IdempotencyStore(Protocol):
    """Records which consequential actions have already been claimed."""

    def claim(self, key: str) -> bool:
        """Atomically claim a key. False when it was already claimed."""
        ...

    def record(self, key: str, outcome: ExecutionOutcome) -> None: ...

    def get(self, key: str) -> ExecutionOutcome | None: ...

    def release(self, key: str) -> None:
        """Release a claim that did not complete, so a retry may proceed."""
        ...


class InMemoryIdempotencyStore:
    """Process-local store. No new infrastructure, by design.

    Sufficient for a single-process application and honest about its limits: it
    does not survive a restart and does not coordinate across processes. Phase
    18.4's durable workflow state is where that changes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed: set[str] = set()
        self._outcomes: dict[str, ExecutionOutcome] = {}

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
            return True

    def record(self, key: str, outcome: ExecutionOutcome) -> None:
        with self._lock:
            self._outcomes[key] = outcome

    def get(self, key: str) -> ExecutionOutcome | None:
        with self._lock:
            return self._outcomes.get(key)

    def release(self, key: str) -> None:
        with self._lock:
            self._claimed.discard(key)


TimeoutRunner = Callable[[Callable[[], ToolOutput], float], ToolOutput]


def run_with_timeout(work: Callable[[], ToolOutput], timeout_seconds: float) -> ToolOutput:
    """Run `work`, returning control at the deadline.

    Raises:
        ToolTimeoutError: when the deadline passes first. The worker is NOT
            killed — see the module docstring.
    """
    if timeout_seconds <= 0:
        return work()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(work)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exception:
            raise ToolTimeoutError("The tool did not complete within its timeout.") from exception


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ToolExecutor:
    """Executes an ALREADY AUTHORISED tool call, reliably.

    It does not authorise. `approved` is supplied by the caller and reflects a
    decision made elsewhere; the executor's role is to refuse to execute a
    state-changing tool without it, which is defence in depth rather than the
    primary control.
    """

    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    default_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    timeouts: dict[str, float] = field(default_factory=dict)
    store: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    timeout_runner: TimeoutRunner = run_with_timeout
    sleeper: Callable[[float], None] = time.sleep

    def timeout_for(self, tool_name: str) -> float:
        """The per-tool timeout, falling back to the default."""
        return self.timeouts.get(tool_name, self.default_timeout_seconds)

    def execute(
        self,
        definition: ToolDefinition,
        payload: ToolInput,
        *,
        approved: bool = False,
        tool_call_id: str | None = None,
    ) -> ExecutionOutcome:
        """Execute one authorised call with timeout, bounded retry and idempotency.

        `tool_call_id` may be supplied by a caller that has already recorded the
        call in an audit trail, so the id on the trajectory event and the id on
        the execution outcome are the same id. Left unset, one is minted here.
        """
        tool_call_id = tool_call_id or f"tc-{uuid.uuid4().hex[:16]}"

        if definition.risk is ToolRiskLevel.STATE_CHANGING and not approved:
            return self._denied(tool_call_id, definition.name)

        key = (
            idempotency_key_for(definition.name, payload)
            if definition.risk is ToolRiskLevel.STATE_CHANGING
            else None
        )
        if key is not None and not self.store.claim(key):
            # This exact action has already been claimed. Returning the recorded
            # outcome rather than re-running is the whole point: a repeated
            # request must not produce a second consequential action.
            return self._duplicate(tool_call_id, definition.name, key)

        try:
            return self._attempt_loop(tool_call_id, definition, payload, key)
        except BaseException:
            if key is not None:
                self.store.release(key)
            raise

    def _attempt_loop(
        self,
        tool_call_id: str,
        definition: ToolDefinition,
        payload: ToolInput,
        key: str | None,
    ) -> ExecutionOutcome:
        records: list[ToolExecutionRecord] = []
        timeout = self.timeout_for(definition.name)

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            delay = self.retry_policy.delay_before(attempt)
            if delay > 0:
                self.sleeper(delay)

            started_at = _now()
            started = time.perf_counter()
            try:
                output = self.timeout_runner(lambda: definition.tool.run(payload), timeout)
            except BaseException as error:  # noqa: BLE001 — classified, then re-raised or retried
                category = category_of(error)
                latency = (time.perf_counter() - started) * 1000.0
                records.append(
                    ToolExecutionRecord(
                        tool_call_id=tool_call_id,
                        tool_name=definition.name,
                        attempt=attempt,
                        status=ExecutionStatus.FAILED,
                        started_at=started_at,
                        completed_at=_now(),
                        latency_ms=latency,
                        failure_category=category,
                        idempotency_key=key,
                    )
                )
                if self.retry_policy.should_retry(category, attempt):
                    continue

                outcome = ExecutionOutcome(
                    tool_call_id=tool_call_id,
                    tool_name=definition.name,
                    status=ExecutionStatus.FAILED,
                    output=None,
                    records=tuple(records),
                    failure_category=category,
                    idempotency_key=key,
                )
                if key is not None:
                    # A consequential action that definitively failed keeps its
                    # claim: whether it is safe to repeat is a human's decision,
                    # not something to infer from an exception type.
                    self.store.record(key, outcome)
                return outcome

            latency = (time.perf_counter() - started) * 1000.0

            # A result is only a result if it is the declared type. Checked here
            # rather than trusted, because everything downstream — the scope
            # fields, the evidence, the chunks an answer rests on — reads
            # attributes off this object.
            if not isinstance(output, definition.tool.output_model):
                records.append(
                    ToolExecutionRecord(
                        tool_call_id=tool_call_id,
                        tool_name=definition.name,
                        attempt=attempt,
                        status=ExecutionStatus.FAILED,
                        started_at=started_at,
                        completed_at=_now(),
                        latency_ms=latency,
                        failure_category=ToolFailureCategory.MALFORMED_OUTPUT,
                        idempotency_key=key,
                    )
                )
                malformed = ExecutionOutcome(
                    tool_call_id=tool_call_id,
                    tool_name=definition.name,
                    status=ExecutionStatus.FAILED,
                    output=None,
                    records=tuple(records),
                    failure_category=ToolFailureCategory.MALFORMED_OUTPUT,
                    idempotency_key=key,
                )
                if key is not None:
                    self.store.record(key, malformed)
                return malformed

            records.append(
                ToolExecutionRecord(
                    tool_call_id=tool_call_id,
                    tool_name=definition.name,
                    attempt=attempt,
                    status=ExecutionStatus.SUCCEEDED,
                    started_at=started_at,
                    completed_at=_now(),
                    latency_ms=latency,
                    idempotency_key=key,
                )
            )
            outcome = ExecutionOutcome(
                tool_call_id=tool_call_id,
                tool_name=definition.name,
                status=ExecutionStatus.SUCCEEDED,
                output=output,
                records=tuple(records),
                idempotency_key=key,
            )
            if key is not None:
                self.store.record(key, outcome)
            return outcome

        # Reached only when the budget was exhausted by retryable failures.
        last = records[-1] if records else None
        outcome = ExecutionOutcome(
            tool_call_id=tool_call_id,
            tool_name=definition.name,
            status=ExecutionStatus.FAILED,
            output=None,
            records=tuple(records),
            failure_category=last.failure_category
            if last
            else ToolFailureCategory.PERMANENT_FAILURE,
            idempotency_key=key,
        )
        if key is not None:
            self.store.record(key, outcome)
        return outcome

    def _denied(self, tool_call_id: str, tool_name: str) -> ExecutionOutcome:
        now = _now()
        record = ToolExecutionRecord(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            attempt=1,
            status=ExecutionStatus.DENIED,
            started_at=now,
            completed_at=now,
            latency_ms=0.0,
            failure_category=ToolFailureCategory.POLICY_DENIED,
        )
        return ExecutionOutcome(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status=ExecutionStatus.DENIED,
            output=None,
            records=(record,),
            failure_category=ToolFailureCategory.POLICY_DENIED,
        )

    def _duplicate(self, tool_call_id: str, tool_name: str, key: str) -> ExecutionOutcome:
        now = _now()
        record = ToolExecutionRecord(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            attempt=1,
            status=ExecutionStatus.DUPLICATE_BLOCKED,
            started_at=now,
            completed_at=now,
            latency_ms=0.0,
            idempotency_key=key,
        )
        previous = self.store.get(key)
        return ExecutionOutcome(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status=ExecutionStatus.DUPLICATE_BLOCKED,
            output=previous.output if previous else None,
            records=(record,),
            idempotency_key=key,
        )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TOOL_TIMEOUT_SECONDS",
    "RETRYABLE_CATEGORIES",
    "ExecutionOutcome",
    "ExecutionStatus",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "InvalidToolInputError",
    "PermanentToolError",
    "PolicyDeniedError",
    "RetryPolicy",
    "ToolExecutionRecord",
    "ToolExecutor",
    "ToolFailureCategory",
    "ToolTimeoutError",
    "TransientToolError",
    "idempotency_key_for",
    "run_with_timeout",
]
