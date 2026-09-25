"""Deterministic doubles for the provider boundary.

Nothing here touches a network, a credential or an Azure SDK. The whole point is
that the lab's behaviour — schema enforcement, error mapping, telemetry shape —
is provable on a laptop with no cloud access at all.
"""

from __future__ import annotations

from typing import Any

from foundry_capability_lab.domain import (
    OperationalRiskAssessment,
    RiskClassification,
    Severity,
)
from foundry_capability_lab.telemetry import (
    InvocationResult,
    InvocationTelemetry,
    ModelMetadata,
    TokenUsage,
)


def sample_assessment() -> OperationalRiskAssessment:
    """A valid assessment used wherever a success path needs a payload."""
    return OperationalRiskAssessment(
        risk_classification=RiskClassification.SERVICE_DEGRADATION,
        severity=Severity.MEDIUM,
        rationale="Repeated ingestion failures with no consumer impact reported yet.",
        requires_escalation=False,
        evidence_ids=["ALERT-4471", "INC-2208"],
    )


class FakeHeaders:
    """Minimal mapping stand-in for an HTTP header collection."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    def get(self, key: str) -> str | None:
        return self._values.get(key)


class FakeResponse:
    """Stand-in for an SDK response object carrying a status and headers."""

    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = FakeHeaders(headers)


class FakeAPIError(Exception):
    """Stand-in for an OpenAI SDK APIStatusError.

    Mirrors only the attributes the adapter actually reads, so the tests do not
    depend on the SDK's internal exception hierarchy.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.response = FakeResponse(status_code, headers)


class FakeTimeoutError(Exception):
    """Stand-in for the SDK's APITimeoutError (matched by class name)."""


class FakeUsage:
    """Stand-in for a Responses API usage object."""

    def __init__(self, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens


class FakeParsedResponse:
    """Stand-in for a `responses.parse` result."""

    def __init__(
        self,
        output_parsed: Any,
        *,
        model: str = "gpt-4.1-mini",
        response_id: str = "resp_fake_0001",
        usage: FakeUsage | None = None,
    ) -> None:
        self.output_parsed = output_parsed
        self.model = model
        self.id = response_id
        self.usage = usage


class FakeResponsesNamespace:
    """Stand-in for `client.responses`."""

    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


class FakeOpenAIClient:
    """Stand-in for an AzureOpenAI client, exposing only `.responses`."""

    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.responses = FakeResponsesNamespace(result, error)


class StubProvider:
    """A RiskAssessmentProvider that returns or raises whatever it is given."""

    def __init__(
        self,
        result: InvocationResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._result = result
        self._error = error

    def assess(self, observation: str) -> InvocationResult:
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise AssertionError("StubProvider needs either a result or an error")
        return self._result


def sample_result(latency_ms: float = 412.5) -> InvocationResult:
    """A complete successful InvocationResult for entry-point tests."""
    return InvocationResult(
        assessment=sample_assessment(),
        telemetry=InvocationTelemetry(
            succeeded=True,
            latency_ms=latency_ms,
            model_metadata=ModelMetadata(deployment="gpt-4-1-mini", model="gpt-4.1-mini"),
            token_usage=TokenUsage(input_tokens=120, output_tokens=48, total_tokens=168),
            request_id="resp_fake_0001",
        ),
    )
