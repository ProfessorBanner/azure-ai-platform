"""Provider boundary: the protocol every caller uses, and the Foundry adapter.

`RiskAssessmentProvider` is the seam. Tests substitute a deterministic fake for
it, and Phase 17 is expected to define its own `FoundryLLMProvider` against a
similar interface. Everything Azure-, OpenAI- or HTTP-specific lives below the
protocol and must not escape upward.

Two design points worth stating explicitly:

1. `classify_exception` is a PURE function from an exception to a typed lab
   error. Keeping it separate from the call path means the whole failure taxonomy
   is unit-testable without a network, a credential or a mocked HTTP stack.

2. Nothing here logs. The adapter returns telemetry and raises typed errors; the
   decision to print anything belongs to the entry point. That is what keeps the
   prompt and the response body out of logs by construction rather than by
   remembering to redact.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from foundry_capability_lab.config import LabConfig, assert_no_api_key_configured
from foundry_capability_lab.domain import OperationalRiskAssessment
from foundry_capability_lab.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    InvalidStructuredOutputError,
    LabError,
    NetworkDeniedError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from foundry_capability_lab.telemetry import (
    InvocationResult,
    InvocationTelemetry,
    ModelMetadata,
    TokenUsage,
)

SYSTEM_INSTRUCTIONS = (
    "You are an operations risk triage assistant for a data platform. "
    "Classify the single observation you are given. Base the assessment only on "
    "the observation; do not speculate beyond it. Populate evidence_ids only "
    "with identifiers that appear verbatim in the observation."
)


@runtime_checkable
class RiskAssessmentProvider(Protocol):
    """Anything that can turn an observation into a validated assessment."""

    def assess(self, observation: str) -> InvocationResult:
        """Classify one observation.

        Raises:
            LabError: a typed failure; never a raw provider exception.
        """
        ...


def _status_code_of(exception: BaseException) -> int | None:
    """Best-effort HTTP status extraction that tolerates SDK shape changes."""
    for attribute in ("status_code", "status"):
        value = getattr(exception, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exception, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _request_id_of(exception: BaseException) -> str | None:
    """Best-effort correlation-id extraction.

    The OpenAI SDK exposes `request_id`; Azure surfaces `apim-request-id` or
    `x-ms-request-id` headers. Any of them is enough to correlate with a support
    case or a Log Analytics record, and all are safe to log.
    """
    request_id = getattr(exception, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    response = getattr(exception, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        for header in ("apim-request-id", "x-ms-request-id", "x-request-id"):
            try:
                value = headers.get(header)
            except (AttributeError, TypeError):
                continue
            if isinstance(value, str) and value:
                return value
    return None


def _looks_like_ip_denial(exception: BaseException) -> bool:
    """Distinguish an IP-rule denial from a missing role assignment.

    Both arrive as HTTP 403. Azure's IP denial carries a distinctive body, so
    match on that rather than guessing — the two have completely different
    remedies (add a CIDR vs. grant a role).
    """
    text = str(exception).lower()
    markers = (
        "not allowed to access",
        "public network access is disabled",
        "virtual network",
        "ip rules",
        "forbidden by policy",
        "accessdenied",
    )
    return any(marker in text for marker in markers)


def classify_exception(exception: BaseException) -> LabError:
    """Map any provider exception onto a typed lab error.

    Pure and side-effect free. The returned error message never embeds the
    original exception's body, only its type name and status, because a provider
    error body can echo request content.
    """
    if isinstance(exception, LabError):
        return exception

    request_id = _request_id_of(exception)
    status = _status_code_of(exception)
    name = type(exception).__name__

    # Timeouts frequently arrive with no status at all.
    if isinstance(exception, TimeoutError) or "timeout" in name.lower():
        return TimeoutError_("The request exceeded the configured timeout.", request_id=request_id)

    if status == 401:
        return AuthenticationError(
            "Entra ID authentication failed (401). Run `az login`, and confirm "
            "the token audience matches AZURE_OPENAI_AUTH_SCOPE.",
            request_id=request_id,
        )

    if status == 403:
        if _looks_like_ip_denial(exception):
            return NetworkDeniedError(
                "The Foundry account refused this caller's network address (403). "
                "The account is default-deny; add the caller's public egress "
                "address to allowed_ip_cidrs and re-apply.",
                request_id=request_id,
            )
        return AuthorizationError(
            "Authenticated, but not authorised for the data plane (403). The "
            "principal likely lacks the `Foundry User` role on the Foundry "
            "account. Owner/Contributor do not grant Foundry data actions.",
            request_id=request_id,
        )

    if status == 404:
        # Structural, not transient. On this data plane a 404 means the endpoint
        # path or the deployment name is wrong; retrying or continuing the run
        # cannot help, so it is classified as configuration and aborts the run.
        return ConfigurationError(
            "Endpoint or deployment not found (404). Check that "
            "AZURE_OPENAI_ENDPOINT ends with /openai/v1/ and is the OpenAI host "
            "rather than the generic account host, and that "
            "AZURE_OPENAI_DEPLOYMENT names an existing deployment.",
            request_id=request_id,
        )

    if status == 429:
        retry_after: float | None = None
        response = getattr(exception, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                raw = headers.get("retry-after")
            except (AttributeError, TypeError):
                raw = None
            if raw is not None:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        return RateLimitedError(
            "Rate limited (429). The deployment's provisioned capacity was "
            "exceeded; raise deployment_capacity or slow the caller.",
            request_id=request_id,
            retry_after_seconds=retry_after,
        )

    if isinstance(exception, ValidationError):
        return InvalidStructuredOutputError(
            "The response did not satisfy OperationalRiskAssessment.",
            request_id=request_id,
        )

    status_text = f" (status {status})" if status is not None else ""
    return ProviderError(f"Provider call failed{status_text}: {name}.", request_id=request_id)


def _usage_from(raw_usage: Any) -> TokenUsage:
    """Read token counts defensively; absent counts stay None."""
    if raw_usage is None:
        return TokenUsage()

    def read(*names: str) -> int | None:
        for candidate in names:
            value = getattr(raw_usage, candidate, None)
            if isinstance(value, int):
                return value
        return None

    return TokenUsage(
        input_tokens=read("input_tokens", "prompt_tokens"),
        output_tokens=read("output_tokens", "completion_tokens"),
        total_tokens=read("total_tokens"),
    )


class FoundryRiskAssessmentProvider:
    """Adapter over the Azure OpenAI / Foundry Responses API v1 surface.

    Authenticates with Microsoft Entra ID only. The constructor accepts an
    already-built client so the network boundary stays injectable; use
    `from_config` for the real, keyless client.
    """

    def __init__(self, client: Any, config: LabConfig) -> None:
        self._client = client
        self._config = config

    @classmethod
    def from_config(cls, config: LabConfig) -> FoundryRiskAssessmentProvider:
        """Build a keyless client using DefaultAzureCredential.

        DefaultAzureCredential is chosen because the same code must work for a
        developer running `az login` locally and for a managed identity later,
        with no branching and no secret in either case.
        """
        # Imported lazily so the module (and therefore the whole test suite)
        # stays importable without the Azure SDKs installed.
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI

        assert_no_api_key_configured()

        token_provider = get_bearer_token_provider(DefaultAzureCredential(), config.auth_scope)

        client = AzureOpenAI(
            base_url=config.endpoint,
            azure_ad_token_provider=token_provider,
            api_version="preview",
            timeout=config.timeout_seconds,
            max_retries=0,  # the lab measures one call; retries would distort latency
        )
        return cls(client, config)

    def assess(self, observation: str) -> InvocationResult:
        """Classify one observation, capturing latency and usage either way."""
        metadata = ModelMetadata(deployment=self._config.deployment)
        started = time.perf_counter()

        try:
            response = self._client.responses.parse(
                model=self._config.deployment,
                instructions=SYSTEM_INSTRUCTIONS,
                input=observation,
                text_format=OperationalRiskAssessment,
            )
        except BaseException as exception:  # noqa: BLE001 - re-raised as a typed error
            raise classify_exception(exception) from exception

        latency_ms = (time.perf_counter() - started) * 1000.0

        assessment = getattr(response, "output_parsed", None)
        if assessment is None:
            raise InvalidStructuredOutputError(
                "The provider returned no parsed output for the requested schema.",
                request_id=getattr(response, "id", None),
            )
        if not isinstance(assessment, OperationalRiskAssessment):
            # Re-validate rather than trust the SDK's parse: the schema contract
            # is the lab's, not the SDK's.
            try:
                assessment = OperationalRiskAssessment.model_validate(assessment)
            except ValidationError as exception:
                raise InvalidStructuredOutputError(
                    "The response did not satisfy OperationalRiskAssessment.",
                    request_id=getattr(response, "id", None),
                ) from exception

        telemetry = InvocationTelemetry(
            succeeded=True,
            latency_ms=latency_ms,
            model_metadata=ModelMetadata(
                deployment=metadata.deployment,
                model=getattr(response, "model", None),
            ),
            token_usage=_usage_from(getattr(response, "usage", None)),
            request_id=getattr(response, "id", None),
        )
        return InvocationResult(assessment=assessment, telemetry=telemetry)
