"""The policy layer: deterministic, total, fail-closed. No model involvement.

WHAT THIS ENFORCES
------------------
Three separate things, deliberately not collapsed:

  1. `screen_request`  — is this question one the agent will answer at all?
  2. `authorise`       — is this tool call permitted?
  3. `scan_untrusted_text` — does retrieved content contain instructions?

WHY NOT ASK A MODEL WHETHER A REQUEST IS SAFE
----------------------------------------------
Because the answer would be a guess produced by the same class of system whose
output is being checked, and vulnerable to the same injection that may have
produced it. An attacker who can influence the request can influence the
reviewer. A lookup in a version-controlled table cannot be talked round.

THE INJECTION POSTURE
---------------------
Two fixture scenarios embed instructions inside monitoring rows and runbook
text. The defence is NOT detection — a good enough injection will evade any
pattern list. The defence is that retrieved content is never on an instruction
path at all: no tool argument, no control-flow branch and no policy decision is
ever derived from retrieved text. `scan_untrusted_text` exists to ANNOTATE such
content as a limitation on the diagnosis, so an operator is told the source
looked tampered with. It is a smoke alarm, not a lock, and treating it as a lock
would be the actual vulnerability.

The lock is architectural: `agent.py` selects tools from the question alone,
never from tool output, and the fixed tool sequence cannot be extended by
anything a tool returns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ml_platform_operations_agent.domain import (
    MAX_MODEL_NAME_CHARS,
    MAX_QUESTION_CHARS,
    RefusalReason,
    ToolRiskLevel,
)

# --- Request screening ------------------------------------------------------
#
# Patterns describe INTENT TO MUTATE, matched against the user's own request.
# They are word-boundary anchored: a substring match would refuse "the
# retraining_data table", which is a legitimate thing to ask about.

_STATE_CHANGING_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bretrain(?:ing)?\s+(?:the\s+|this\s+|it\b|model|now)",
        r"\bplease\s+retrain\b",
        r"\b(?:trigger|start|run|launch|kick\s*off)\s+(?:a\s+|the\s+)?(?:retrain|job|pipeline|run)",
        r"\b(?:promote|demote)\b",
        r"\b(?:set|move|change|update|point|switch|assign)\s+(?:the\s+)?(?:champion|candidate|alias)",
        r"\balias\s+(?:to|change|move|update)\b",
        r"\bmake\s+.{0,40}?\bchampion\b",
        r"\b(?:delete|drop|truncate|insert|update|alter|create)\s+(?:table|schema|catalog|model)",
        r"\b(?:register|deploy|roll\s*back|rollback)\s+(?:a\s+|the\s+)?(?:model|version)",
        r"\backnowledge\s+(?:the\s+)?alert",
        r"\bunpause\b|\bresume\s+the\s+schedule\b",
    )
)

#: Attempts to supply executable material rather than ask a question.
_UNSAFE_INSTRUCTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bselect\b[\s\S]{0,80}\bfrom\b",
        r"\b(?:union\s+all|union)\s+select\b",
        r";\s*(?:drop|delete|update|insert|alter|create|grant|revoke)\b",
        r"\bgrant\b|\brevoke\b",
        r"--\s*$",
        r"/\*[\s\S]*?\*/",
        r"\bexec(?:ute)?\s*\(",
        r"\bdbfs:/|\babfss://|\bs3://",
        r"(?:^|[\s\"\x27(])/(?:Workspace|Volumes|Repos)/",
        r"\bspark\.sql\s*\(",
        r"\bdbutils\.",
    )
)

#: Instruction-shaped text inside RETRIEVED content. Used only to annotate, per
#: the module docstring.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\b",
        r"\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|instructions)\b",
        r"\byou\s+are\s+now\b",
        r"\bnew\s+instructions?\b",
        r"\bsystem\s*(?:prompt|message)\b",
        r"\b(?:must|should|please)\s+(?:now\s+)?(?:retrain|promote|delete|grant|run|execute)\b",
        r"\bas\s+an?\s+(?:ai|assistant|agent)\b",
        r"\boverride\s+(?:the\s+)?(?:policy|safety|guard)",
        r"</?(?:system|instruction|prompt)>",
    )
)


@dataclass(frozen=True, slots=True)
class RequestVerdict:
    """The outcome of screening one request."""

    allowed: bool
    refusal_reason: RefusalReason | None = None
    #: Names the RULE, never the offending text — this string is logged and the
    #: request may be attacker-controlled.
    detail: str = ""


def screen_request(question: str, model_name: str = "") -> RequestVerdict:
    """Decide whether the agent will engage with this request at all.

    Order matters. Length is checked FIRST so an enormous payload is rejected
    before any regex runs over it, and a mutation request is recognised before
    an unsafe-instruction check that might match the same text with a less
    precise reason.
    """
    if len(question) > MAX_QUESTION_CHARS:
        return RequestVerdict(
            allowed=False,
            refusal_reason=RefusalReason.INPUT_REJECTED,
            detail=f"the request exceeds the {MAX_QUESTION_CHARS}-character limit",
        )

    if len(model_name) > MAX_MODEL_NAME_CHARS:
        return RequestVerdict(
            allowed=False,
            refusal_reason=RefusalReason.INPUT_REJECTED,
            detail=f"the model identifier exceeds the {MAX_MODEL_NAME_CHARS}-character limit",
        )

    if not question.strip():
        return RequestVerdict(
            allowed=False,
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
            detail="the request is empty",
        )

    # Executable material is checked BEFORE mutation intent. `DROP TABLE x`
    # matches both, and "you supplied SQL" is the more specific and more
    # actionable finding than "you expressed intent to change something" —
    # the first names what arrived, the second infers what was meant.
    for pattern in _UNSAFE_INSTRUCTION_PATTERNS:
        if pattern.search(question):
            return RequestVerdict(
                allowed=False,
                refusal_reason=RefusalReason.UNSAFE_INSTRUCTION,
                detail="the request supplies executable material rather than a question",
            )

    for pattern in _STATE_CHANGING_PATTERNS:
        if pattern.search(question):
            return RequestVerdict(
                allowed=False,
                refusal_reason=RefusalReason.STATE_CHANGING_REQUEST,
                detail="the request asks for a state-changing ML operation",
            )

    return RequestVerdict(allowed=True)


# --- Tool authorisation -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolVerdict:
    """The outcome of authorising one tool call."""

    allowed: bool
    tool_name: str
    risk_level: ToolRiskLevel | None = None
    detail: str = ""


def authorise(
    tool_name: str,
    arguments: dict[str, object],
    registry: object,
) -> ToolVerdict:
    """Rule on one tool call. Deterministic and side-effect free.

    Rules, in order:

        empty name          -> DENY
        not registered      -> DENY   (never guessed at; a near-miss resolved
                                       generously is how a caller reaches a tool
                                       it was not offered)
        not allow-listed    -> DENY
        arguments invalid   -> DENY
        risk READ_ONLY      -> ALLOW
        anything else       -> DENY   (no default-allow branch exists)

    A STATE_CHANGING tool denies rather than requesting approval. Phase 19.2
    registers none, and an approval path would imply the agent could carry such
    an action out once approved — which it cannot, and which the README must not
    have to explain away.
    """
    from ml_platform_operations_agent.tools import ToolRegistry

    if not isinstance(registry, ToolRegistry):
        raise TypeError("authorise requires a ToolRegistry")

    name = tool_name.strip()
    if not name:
        return ToolVerdict(allowed=False, tool_name="", detail="a tool call must name a tool")

    definition = registry.get(name)
    if definition is None:
        return ToolVerdict(allowed=False, tool_name=name, detail="the named tool is not registered")

    if not definition.allowed:
        return ToolVerdict(
            allowed=False,
            tool_name=name,
            risk_level=definition.risk,
            detail="the named tool is registered but not permitted in this deployment",
        )

    if definition.risk is not ToolRiskLevel.READ_ONLY:
        return ToolVerdict(
            allowed=False,
            tool_name=name,
            risk_level=definition.risk,
            detail="only read-only tools may be invoked by this agent",
        )

    problem = definition.validate_arguments(arguments)
    if problem is not None:
        # Names the rule and the argument NAME, never its value.
        return ToolVerdict(
            allowed=False,
            tool_name=name,
            risk_level=definition.risk,
            detail=f"tool arguments are not valid: {problem}",
        )

    return ToolVerdict(allowed=True, tool_name=name, risk_level=definition.risk)


# --- Untrusted content ------------------------------------------------------


def scan_untrusted_text(text: str) -> tuple[str, ...]:
    """Return limitation strings for instruction-shaped retrieved content.

    ANNOTATION, NOT ENFORCEMENT. See the module docstring: a determined
    injection will evade this list, and the reason that is acceptable is that
    retrieved text never reaches an instruction path. What this buys is that an
    operator reading the diagnosis is told a source looked tampered with, rather
    than silently trusting a monitoring row someone wrote into.

    The matched text is NEVER echoed back — quoting the injection into the
    diagnosis would carry it to the next reader, which is the delivery mechanism
    the injection wanted in the first place.
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return (
                "A retrieved source contains instruction-like text. It was treated as "
                "data and had no effect on tool selection or the diagnosis; the content "
                "is not reproduced here. Review the source before acting on it.",
            )
    return ()


def redact(text: str, limit: int = 200) -> str:
    """Reduce free text to something safe to place in a summary.

    Collapses whitespace, strips control characters and truncates. Applied to
    every value that originated outside this process before it appears in a
    `Diagnosis`, so an over-long or newline-stuffed monitoring `reasons` field
    cannot reshape the rendered answer.
    """
    collapsed = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    collapsed = re.sub(r"\s+", " ", collapsed).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


__all__ = [
    "RequestVerdict",
    "ToolVerdict",
    "authorise",
    "redact",
    "scan_untrusted_text",
    "screen_request",
]
