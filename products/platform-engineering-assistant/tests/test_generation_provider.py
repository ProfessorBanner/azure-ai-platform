"""The provider boundary: the fake, the draft contract, and Azure error mapping.

The Azure adapter is exercised through `classify_exception` and an injected fake
client. No test constructs a credential or opens a socket.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from platform_engineering_assistant.domain import RefusalReason
from platform_engineering_assistant.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    InvalidStructuredOutputError,
    NetworkDeniedError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from platform_engineering_assistant.generation.azure_openai import classify_exception
from platform_engineering_assistant.generation.draft import DraftDisposition, GroundedDraft
from platform_engineering_assistant.generation.fake import (
    FakeBehaviour,
    FakeGenerationProvider,
)
from platform_engineering_assistant.generation.protocol import (
    GenerationProvider,
    GenerationRequest,
)

REQUEST = GenerationRequest(
    system_prompt="system",
    context="[chunk_id: a::h::0::0]\ncontent:\nbody\n--- end of chunk ---",
    question="a question about the platform",
    max_answer_chars=4000,
)


# --- the draft contract -----------------------------------------------------


def test_a_draft_carries_only_disposition_answer_citations_and_reason() -> None:
    """Provenance is server-resolved; the model is never asked for it."""
    assert set(GroundedDraft.model_fields) == {
        "disposition",
        "answer",
        "cited_chunk_ids",
        "refusal_reason",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        {"doc_path": "docs/adr/0005.md"},
        {"doc_title": "ADR 0005"},
        {"line_numbers": [10, 20]},
        {"authority": "adr"},
        {"url": "https://example.invalid"},
        {"model": "gpt-4o"},
        {"request_id": "abc"},
    ],
)
def test_a_draft_cannot_supply_trusted_provenance(forbidden: dict[str, object]) -> None:
    """A fabricated path beside a real answer is worse than a fabricated answer."""
    with pytest.raises(ValidationError):
        GroundedDraft.model_validate({"disposition": "answered", "answer": "text", **forbidden})


def test_a_malformed_draft_is_rejected_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        GroundedDraft.model_validate({"disposition": "maybe", "answer": "text"})


def test_a_draft_is_immutable() -> None:
    draft = GroundedDraft(
        disposition=DraftDisposition.REFUSED, refusal_reason=RefusalReason.OUT_OF_SCOPE
    )
    with pytest.raises(ValidationError):
        draft.answer = "sneaky"


# --- the fake ---------------------------------------------------------------


def test_the_fake_satisfies_the_provider_protocol() -> None:
    assert isinstance(FakeGenerationProvider(), GenerationProvider)


def test_the_fake_is_deterministic() -> None:
    first = FakeGenerationProvider().generate(REQUEST)
    second = FakeGenerationProvider().generate(REQUEST)
    assert first.draft == second.draft


def test_the_fake_can_only_cite_what_the_context_offered() -> None:
    """It parses the rendered context, so it exercises the real contract."""
    outcome = FakeGenerationProvider().generate(REQUEST)
    assert outcome.draft.cited_chunk_ids == ["a::h::0::0"]


def test_the_fake_reports_telemetry() -> None:
    telemetry = FakeGenerationProvider().generate(REQUEST).telemetry
    assert telemetry.provider == "fake-deterministic"
    assert telemetry.total_tokens is not None
    assert telemetry.latency_ms >= 0


def test_the_fake_raises_the_configured_error() -> None:
    with pytest.raises(ProviderError):
        FakeGenerationProvider(error=ProviderError("boom")).generate(REQUEST)


def test_the_fake_records_the_requests_it_received() -> None:
    provider = FakeGenerationProvider()
    provider.generate(REQUEST)
    assert provider.calls == [REQUEST]


@pytest.mark.parametrize("behaviour", list(FakeBehaviour))
def test_every_fake_behaviour_produces_a_structurally_valid_draft(
    behaviour: FakeBehaviour,
) -> None:
    """The fake models misbehaviour, not malformed JSON: the schema still holds."""
    outcome = FakeGenerationProvider(behaviour).generate(REQUEST)
    assert isinstance(outcome.draft, GroundedDraft)


# --- Azure error mapping (pure; no client, no network) ----------------------


class FakeAPIError(Exception):
    """Mirrors only the attributes the adapter reads, so tests do not depend on
    the OpenAI SDK's internal exception hierarchy."""

    def __init__(self, status_code: int, message: str = "failure") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = None
        self.request_id = None


def status_error(status: int, message: str = "failure") -> FakeAPIError:
    return FakeAPIError(status, message)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (403, AuthorizationError),
        (404, ConfigurationError),
        (429, RateLimitedError),
        (500, ProviderError),
        (503, ProviderError),
    ],
)
def test_http_status_maps_to_a_typed_error(status: int, expected: type[Exception]) -> None:
    assert isinstance(classify_exception(status_error(status)), expected)


def test_an_ip_denial_is_distinguished_from_a_missing_role() -> None:
    """Both are 403 and need completely different fixes."""
    denial = status_error(403, "Public network access is disabled for this IP.")
    assert isinstance(classify_exception(denial), NetworkDeniedError)
    assert isinstance(classify_exception(status_error(403, "no permission")), AuthorizationError)


def test_a_timeout_is_mapped_without_a_status() -> None:
    assert isinstance(classify_exception(TimeoutError("deadline")), TimeoutError_)


def test_a_validation_error_becomes_invalid_structured_output() -> None:
    try:
        GroundedDraft.model_validate({"disposition": "nonsense"})
    except ValidationError as exc:
        assert isinstance(classify_exception(exc), InvalidStructuredOutputError)


def test_a_typed_error_passes_through_unchanged() -> None:
    original = AuthorizationError("already typed")
    assert classify_exception(original) is original


def test_error_messages_never_embed_the_provider_body() -> None:
    """A provider error body can echo request content."""
    secret = "the user asked about ACCOUNT-98765"
    mapped = classify_exception(status_error(500, secret))
    assert secret not in str(mapped)
