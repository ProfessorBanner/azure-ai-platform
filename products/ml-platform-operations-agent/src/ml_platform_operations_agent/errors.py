"""Typed failure taxonomy for the ML platform operations agent.

Every failure carries a machine-readable category so a caller can branch without
parsing a message string.

REDACTION RULE, APPLIED THROUGHOUT
----------------------------------
Every message in this module is written on the assumption that it WILL be
logged. No message may contain a question, a row of monitoring data, a runbook
excerpt, a credential, a workspace path, or the value that caused the failure.
Errors name the RULE that was broken and the LOCATION it was broken at — never
the offending value.

This matters more here than in a documentation assistant. The values flowing
through this agent are operational telemetry and user-supplied model names, and
at least two fixture scenarios embed prompt injection inside otherwise ordinary
monitoring rows. An error that echoed the offending row would carry the
injection into the log, and from there into whatever reads the log next.

ABSENCE IS NOT A FAILURE
------------------------
The single most important distinction in this module is what is NOT here.
A missing `monitoring_history` table does not raise. Phase 19.2a established
that DEV has no monitoring tables at all, so absence is the ordinary case, and
an agent that raised on it would be unusable in the environment it was built
for. Absence flows through `EvidenceAbsence` (domain.py) to an
`insufficient_evidence` diagnosis. Only a source that was expected to answer and
malfunctioned raises `EvidenceSourceError`.
"""

from __future__ import annotations

from enum import StrEnum


class FailureCategory(StrEnum):
    """Stable classification of a failure."""

    CONFIGURATION = "configuration"

    #: An evidence source was reachable in principle and failed in a way that is
    #: not "the data is not there". A timeout, a malformed payload, a permission
    #: error. Distinct from absence, which is not a failure at all.
    EVIDENCE_SOURCE = "evidence_source"

    #: The caller's request could not be turned into a bounded question: an
    #: over-long input, an unparseable window, a model name that is not a
    #: governed identifier.
    INVALID_REQUEST = "invalid_request"

    #: Evidence arrived but violated the envelope contract — an unresolvable
    #: identifier, a missing observation timestamp, a duplicated reference.
    #: Fails closed: malformed evidence is discarded, never repaired.
    MALFORMED_EVIDENCE = "malformed_evidence"

    #: The bounded loop hit its iteration ceiling.
    ITERATION_LIMIT = "iteration_limit"


class OperationsAgentError(Exception):
    """Base class. Always carries a category; never carries a value."""

    category: FailureCategory = FailureCategory.CONFIGURATION

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(OperationsAgentError):
    """Configuration that cannot produce a working agent."""

    category = FailureCategory.CONFIGURATION


class EvidenceSourceError(OperationsAgentError):
    """An evidence source malfunctioned.

    NOT raised when a source correctly reports that data does not exist — that
    is `EvidenceAbsence`, and it is an answer rather than an error.
    """

    category = FailureCategory.EVIDENCE_SOURCE


class InvalidRequestError(OperationsAgentError):
    """The request cannot be bounded."""

    category = FailureCategory.INVALID_REQUEST


class MalformedEvidenceError(OperationsAgentError):
    """Evidence violated the envelope contract."""

    category = FailureCategory.MALFORMED_EVIDENCE


__all__ = [
    "ConfigurationError",
    "EvidenceSourceError",
    "FailureCategory",
    "InvalidRequestError",
    "MalformedEvidenceError",
    "OperationsAgentError",
]
