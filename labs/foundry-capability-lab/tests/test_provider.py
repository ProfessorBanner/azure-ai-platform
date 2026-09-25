"""Provider adapter: success path, error mapping, and schema enforcement.

Every test drives the adapter through an injected fake client. No Azure SDK, no
credential and no network is involved.
"""

from __future__ import annotations

import pytest

from foundry_capability_lab.config import LabConfig
from foundry_capability_lab.domain import OperationalRiskAssessment
from foundry_capability_lab.errors import (
    AuthenticationError,
    AuthorizationError,
    FailureCategory,
    InvalidStructuredOutputError,
    NetworkDeniedError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from foundry_capability_lab.provider import (
    FoundryRiskAssessmentProvider,
    RiskAssessmentProvider,
    classify_exception,
)
from tests.fakes import (
    FakeAPIError,
    FakeOpenAIClient,
    FakeParsedResponse,
    FakeTimeoutError,
    FakeUsage,
    sample_assessment,
)

CONFIG = LabConfig(
    endpoint="https://example-resource.openai.azure.com/openai/v1/",
    deployment="gpt-4-1-mini",
)
OBSERVATION = "Alert ALERT-1 fired."


def build_provider(
    result: object = None, error: BaseException | None = None
) -> FoundryRiskAssessmentProvider:
    return FoundryRiskAssessmentProvider(FakeOpenAIClient(result, error), CONFIG)


# --- success path -----------------------------------------------------------


def test_successful_call_returns_validated_result() -> None:
    response = FakeParsedResponse(
        sample_assessment(),
        usage=FakeUsage(input_tokens=120, output_tokens=48, total_tokens=168),
    )
    result = build_provider(response).assess(OBSERVATION)

    assert isinstance(result.assessment, OperationalRiskAssessment)
    assert result.telemetry.succeeded is True
    assert result.telemetry.failure_category is None
    assert result.telemetry.token_usage.total_tokens == 168
    assert result.telemetry.request_id == "resp_fake_0001"
    assert result.telemetry.model_metadata.deployment == "gpt-4-1-mini"
    assert result.telemetry.model_metadata.model == "gpt-4.1-mini"
    assert result.telemetry.model_metadata.api_contract == "openai-responses-v1"


def test_latency_is_captured_as_a_positive_duration() -> None:
    result = build_provider(FakeParsedResponse(sample_assessment())).assess(OBSERVATION)
    assert result.telemetry.latency_ms >= 0.0
    # Sanity bound: an in-process fake cannot plausibly take a second.
    assert result.telemetry.latency_ms < 1000.0


def test_absent_usage_leaves_token_counts_none() -> None:
    result = build_provider(FakeParsedResponse(sample_assessment(), usage=None)).assess(OBSERVATION)
    assert result.telemetry.token_usage.input_tokens is None
    assert result.telemetry.token_usage.total_tokens is None


def test_request_targets_the_configured_deployment_and_schema() -> None:
    client = FakeOpenAIClient(FakeParsedResponse(sample_assessment()))
    FoundryRiskAssessmentProvider(client, CONFIG).assess(OBSERVATION)

    (call,) = client.responses.calls
    assert call["model"] == "gpt-4-1-mini"
    assert call["text_format"] is OperationalRiskAssessment
    assert call["input"] == OBSERVATION


def test_provider_satisfies_the_protocol() -> None:
    provider: RiskAssessmentProvider = build_provider(FakeParsedResponse(sample_assessment()))
    assert isinstance(provider, RiskAssessmentProvider)


# --- structured-output enforcement -----------------------------------------


def test_missing_parsed_output_is_rejected() -> None:
    with pytest.raises(InvalidStructuredOutputError) as caught:
        build_provider(FakeParsedResponse(None)).assess(OBSERVATION)
    assert caught.value.category is FailureCategory.INVALID_STRUCTURED_OUTPUT


def test_payload_failing_the_schema_is_rejected() -> None:
    """A dict that does not satisfy the schema must not be accepted."""
    bad = {"risk_classification": "not-a-category", "severity": "high"}
    with pytest.raises(InvalidStructuredOutputError):
        build_provider(FakeParsedResponse(bad)).assess(OBSERVATION)


def test_schema_valid_dict_is_revalidated_and_accepted() -> None:
    payload = sample_assessment().model_dump()
    result = build_provider(FakeParsedResponse(payload)).assess(OBSERVATION)
    assert isinstance(result.assessment, OperationalRiskAssessment)


# --- error mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_type", "expected_category"),
    [
        (401, AuthenticationError, FailureCategory.AUTHENTICATION),
        (403, AuthorizationError, FailureCategory.AUTHORIZATION),
        (429, RateLimitedError, FailureCategory.RATE_LIMITED),
        (500, ProviderError, FailureCategory.PROVIDER_ERROR),
        (503, ProviderError, FailureCategory.PROVIDER_ERROR),
    ],
)
def test_http_status_maps_to_typed_error(
    status: int, expected_type: type[Exception], expected_category: FailureCategory
) -> None:
    error = FakeAPIError("boom", status_code=status, request_id="req-123")
    with pytest.raises(expected_type) as caught:
        build_provider(error=error).assess(OBSERVATION)
    assert caught.value.category is expected_category  # type: ignore[attr-defined]
    assert caught.value.request_id == "req-123"  # type: ignore[attr-defined]


def test_ip_denial_is_distinguished_from_missing_role() -> None:
    """Both are 403; they need different remedies, so they must not collapse."""
    denial = FakeAPIError(
        "Public network access is disabled and request is not allowed to access "
        "the resource from this IP.",
        status_code=403,
    )
    with pytest.raises(NetworkDeniedError) as caught:
        build_provider(error=denial).assess(OBSERVATION)
    assert caught.value.category is FailureCategory.NETWORK_DENIED

    missing_role = FakeAPIError("The principal does not have permission.", status_code=403)
    with pytest.raises(AuthorizationError):
        build_provider(error=missing_role).assess(OBSERVATION)


def test_timeout_is_mapped_even_without_a_status() -> None:
    with pytest.raises(TimeoutError_) as caught:
        build_provider(error=FakeTimeoutError("timed out")).assess(OBSERVATION)
    assert caught.value.category is FailureCategory.TIMEOUT


def test_builtin_timeout_is_mapped() -> None:
    with pytest.raises(TimeoutError_):
        build_provider(error=TimeoutError("deadline exceeded")).assess(OBSERVATION)


def test_rate_limit_retry_after_is_captured() -> None:
    error = FakeAPIError("slow down", status_code=429, headers={"retry-after": "7"})
    with pytest.raises(RateLimitedError) as caught:
        build_provider(error=error).assess(OBSERVATION)
    assert caught.value.retry_after_seconds == 7.0


def test_malformed_retry_after_is_tolerated() -> None:
    error = FakeAPIError("slow down", status_code=429, headers={"retry-after": "soon"})
    with pytest.raises(RateLimitedError) as caught:
        build_provider(error=error).assess(OBSERVATION)
    assert caught.value.retry_after_seconds is None


def test_request_id_is_read_from_azure_headers_when_absent_on_the_exception() -> None:
    error = FakeAPIError("boom", status_code=500, headers={"apim-request-id": "apim-abc"})
    with pytest.raises(ProviderError) as caught:
        build_provider(error=error).assess(OBSERVATION)
    assert caught.value.request_id == "apim-abc"


def test_classify_exception_passes_through_lab_errors() -> None:
    original = AuthorizationError("already typed")
    assert classify_exception(original) is original


def test_unknown_exception_becomes_a_provider_error() -> None:
    with pytest.raises(ProviderError):
        build_provider(error=RuntimeError("something odd")).assess(OBSERVATION)
