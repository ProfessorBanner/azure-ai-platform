"""Typed failure categories for data-plane invocation.

The lab's purpose includes proving that the DISTINCT failure modes of a private,
keyless, IP-restricted endpoint are distinguishable by an operator. A single
generic exception would hide exactly the differences that matter when
diagnosing: a 401 means the token is wrong, a 403 usually means the role
assignment is missing, and a network denial means the caller's address is not on
the account's allow-list. Those need three different fixes.

Every message here is written on the assumption it may be logged, so none of
them may carry a prompt, a token, a credential or response content.
"""

from __future__ import annotations

from enum import StrEnum


class FailureCategory(StrEnum):
    """Stable, machine-readable classification of an invocation failure."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NETWORK_DENIED = "network_denied"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INVALID_STRUCTURED_OUTPUT = "invalid_structured_output"
    PROVIDER_ERROR = "provider_error"
    CONFIGURATION = "configuration"


class LabError(Exception):
    """Base class for every failure the lab reports.

    Carries a category so callers can branch on the failure without parsing a
    message string.
    """

    category: FailureCategory = FailureCategory.PROVIDER_ERROR

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id

    def __str__(self) -> str:
        base = super().__str__()
        if self.request_id:
            return f"[{self.category}] {base} (request_id={self.request_id})"
        return f"[{self.category}] {base}"


class ConfigurationError(LabError):
    """Required configuration is missing or malformed, or a key was supplied."""

    category = FailureCategory.CONFIGURATION


class AuthenticationError(LabError):
    """No usable Entra ID token, or the endpoint rejected it (HTTP 401).

    Usually means `az login` has not been run, or the token was issued for the
    wrong audience.
    """

    category = FailureCategory.AUTHENTICATION


class AuthorizationError(LabError):
    """The token authenticated but the principal lacks data-plane rights (403).

    Usually means the caller is missing the `Foundry User` role on the Foundry
    account. An inherited Owner or Contributor assignment does NOT fix this:
    those roles carry control-plane actions but no Foundry dataActions, so they
    can manage the resource without being able to call it.

    Note Azure also returns 403 when an IP rule denies the caller; the provider
    adapter inspects the response body to separate the two.
    """

    category = FailureCategory.AUTHORIZATION


class NetworkDeniedError(LabError):
    """The account's IP allow-list rejected the caller's address.

    The account is default-deny, so this means the caller's public egress
    address is not in `allowed_ip_cidrs`.
    """

    category = FailureCategory.NETWORK_DENIED


class RateLimitedError(LabError):
    """The deployment's provisioned capacity was exceeded (HTTP 429)."""

    category = FailureCategory.RATE_LIMITED

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.retry_after_seconds = retry_after_seconds


class TimeoutError_(LabError):
    """The request exceeded the configured timeout.

    Named with a trailing underscore to avoid shadowing the builtin
    `TimeoutError`, which would be an unpleasant surprise in `except` clauses.
    """

    category = FailureCategory.TIMEOUT


class InvalidStructuredOutputError(LabError):
    """The response was not valid against the domain schema.

    Deliberately does NOT carry the offending payload: the payload is model
    output derived from the prompt, and the lab does not log response content.
    """

    category = FailureCategory.INVALID_STRUCTURED_OUTPUT


class ProviderError(LabError):
    """Any other provider-side failure (5xx, malformed response, transport)."""

    category = FailureCategory.PROVIDER_ERROR
