"""Typed failure taxonomy for the platform engineering assistant.

Every failure carries a machine-readable category so a caller can branch without
parsing a message string, and so telemetry can count failures by kind.

REDACTION RULE, APPLIED THROUGHOUT
----------------------------------
Every message in this module is written on the assumption that it WILL be
logged. No message may contain a question, an answer, document content, a
credential, a token, or the value that caused the failure. Errors name the
RULE that was broken and the LOCATION it was broken at — never the offending
value. The corpus safety scanner depends on this: an error that quoted the
secret it found would publish the secret it was protecting.
"""

from __future__ import annotations

from enum import StrEnum


class FailureCategory(StrEnum):
    """Stable classification of a failure."""

    CONFIGURATION = "configuration"
    CORPUS = "corpus"

    # Reserved for later 17.1 batches. Declared here so the taxonomy is designed
    # once rather than grown ad hoc, and so telemetry counters are stable.
    RETRIEVAL = "retrieval"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NETWORK_DENIED = "network_denied"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INVALID_STRUCTURED_OUTPUT = "invalid_structured_output"
    UNSUPPORTED_CITATION = "unsupported_citation"
    PROVIDER_ERROR = "provider_error"


class CorpusRule(StrEnum):
    """The specific corpus rule a document violated.

    Finer-grained than FailureCategory because the corpus rules have genuinely
    different remedies: an absolute path is an authoring mistake, a symlink is a
    containment breach, and detected credential material is an incident.
    """

    ABSOLUTE_PATH = "absolute_path"
    PARENT_TRAVERSAL = "parent_traversal"
    OUTSIDE_REPOSITORY = "outside_repository"
    SYMLINK = "symlink"
    NOT_REGULAR_FILE = "not_regular_file"
    NOT_MARKDOWN = "not_markdown"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    DUPLICATE_PATH = "duplicate_path"
    DUPLICATE_DOC_ID = "duplicate_doc_id"
    CREDENTIAL_MATERIAL = "credential_material"


class AssistantError(Exception):
    """Base class for every failure this product reports."""

    category: FailureCategory = FailureCategory.CONFIGURATION

    def __str__(self) -> str:
        return f"[{self.category}] {super().__str__()}"


class ConfigurationError(AssistantError):
    """Configuration is missing, malformed, or out of its permitted range."""

    category = FailureCategory.CONFIGURATION


class CorpusError(AssistantError):
    """A corpus document failed an admission rule.

    Carries the rule and, where known, the repository-relative path. The path is
    safe to report — it is the location of the problem and is already public in
    the manifest. The document's CONTENT never is.
    """

    category = FailureCategory.CORPUS

    def __init__(self, rule: CorpusRule, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.rule = rule
        self.path = path

    def __str__(self) -> str:
        located = f" (path={self.path})" if self.path else ""
        return f"[{self.category}:{self.rule}] {Exception.__str__(self)}{located}"


# --- generation and provider failures (Phase 17.1c) --------------------------
#
# The taxonomy was declared in 17.1a; these are the classes for the categories
# that only became reachable once a provider existed. Each is distinguished
# because each needs a different fix and a different HTTP status: a 401 means
# the token is wrong, a 403 usually means the role assignment is missing, and a
# network denial means the caller's address is not allow-listed.


class AuthenticationError(AssistantError):
    """No usable Entra ID token, or the endpoint rejected it (HTTP 401)."""

    category = FailureCategory.AUTHENTICATION


class AuthorizationError(AssistantError):
    """Authenticated, but the principal lacks data-plane rights (HTTP 403).

    Usually a missing `Foundry User` role on the Foundry account. An inherited
    Owner or Contributor assignment does not help: those carry control-plane
    actions but no Foundry dataActions.
    """

    category = FailureCategory.AUTHORIZATION


class NetworkDeniedError(AssistantError):
    """The account's IP allow-list rejected the caller's address (also HTTP 403)."""

    category = FailureCategory.NETWORK_DENIED


class RateLimitedError(AssistantError):
    """The deployment's provisioned capacity was exceeded (HTTP 429)."""

    category = FailureCategory.RATE_LIMITED

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TimeoutError_(AssistantError):
    """The generation request exceeded its configured timeout.

    Trailing underscore to avoid shadowing the builtin `TimeoutError`, which
    would be an unpleasant surprise in an `except` clause.
    """

    category = FailureCategory.TIMEOUT


class InvalidStructuredOutputError(AssistantError):
    """The model response did not satisfy the grounded-draft schema.

    Deliberately carries no payload: the offending text is model output derived
    from the question and the corpus, and this message is written to be logged.
    """

    category = FailureCategory.INVALID_STRUCTURED_OUTPUT


class ProviderError(AssistantError):
    """Any other provider-side failure (5xx, transport, malformed response)."""

    category = FailureCategory.PROVIDER_ERROR
