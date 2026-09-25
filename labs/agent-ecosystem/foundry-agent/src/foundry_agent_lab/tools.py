"""The lab's tool layer: server-owned risk, a strict schema, and validation.

WHAT MOVED TO FOUNDRY AND WHAT DID NOT
--------------------------------------
Foundry decides WHICH tool to call and with what arguments — it runs the model
and emits a function call. Everything after that stays here:

    Foundry      selects the tool and proposes arguments
    Lab          validates the arguments        <- application-owned
    Lab          decides whether it may run     <- application-owned
    Lab          executes the trusted code      <- application-owned
    Foundry      composes the final answer

This is the same split Phase 18 makes, with the model's proposal arriving over a
different wire. The point of the lab is that the split does not have to move
just because the orchestration did.

RISK IS STILL A PROPERTY OF THE TOOL
------------------------------------
`risk` is declared here, in server-side code, and read from this registry. It is
never sent to Foundry, never present in the function schema, and never read back
from a function call. Phase 19.1b registers exactly one tool and it is
READ_ONLY; a state-changing tool is not merely absent but refused by
construction, and a test asserts it.

THE SCHEMA IS HAND-BUILT, AND THAT IS THE PHASE 18 LESSON APPLIED
-----------------------------------------------------------------
Phase 18 lost a day to a Pydantic-derived schema: `dict[str, str]` rendered as
`additionalProperties: {"type": "string"}` and a bound rendered as
`maxProperties`, and strict Structured Outputs rejected both. So the function
schema here is written explicitly — every property declared, `additionalProperties`
false, every property in `required` — and the LENGTH bounds live in the
validator rather than in the schema, where they would render as `minLength` /
`maxLength`. The typed model still validates; it just is not what is sent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_QUERY_LENGTH = 400
MIN_QUERY_LENGTH = 3

# How many tool calls one turn may perform. Bounded by construction, as a `for`
# over a range rather than a `while`: an unbounded loop against a metered model
# is a spend incident waiting for one bad decision.
MAX_TOOL_CALLS = 2


class ToolRiskLevel(StrEnum):
    """How consequential a tool is. Server-owned; never sent to or read from Foundry."""

    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"


class RejectionReason(StrEnum):
    """Why a proposed call was refused. Recorded; never used to coach the model."""

    UNKNOWN_TOOL = "unknown_tool"
    NOT_ALLOW_LISTED = "not_allow_listed"
    MALFORMED_ARGUMENTS = "malformed_arguments"
    MISSING_ARGUMENT = "missing_argument"
    UNEXPECTED_ARGUMENT = "unexpected_argument"
    ARGUMENT_TOO_SHORT = "argument_too_short"
    ARGUMENT_TOO_LONG = "argument_too_long"
    WRONG_ARGUMENT_TYPE = "wrong_argument_type"
    CALL_LIMIT_EXCEEDED = "call_limit_exceeded"


class ToolRejected(Exception):
    """A proposed call did not survive validation. Nothing was executed."""

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SearchDocsArguments:
    """The validated argument set for `search_platform_docs`."""

    query: str


@dataclass(frozen=True, slots=True)
class LabTool:
    """Trusted metadata for one tool, plus the callable that performs it."""

    name: str
    description: str
    risk: ToolRiskLevel
    run: Callable[[SearchDocsArguments], str]
    allowed: bool = True

    def function_schema(self) -> dict[str, Any]:
        """The tool as Foundry is told about it.

        Name, purpose and the argument contract. Risk is absent: it is not the
        model's business, and sending it would invite an argument the validation
        layer does not participate in.
        """
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to search the approved platform documentation for. "
                            "A natural-language phrase."
                        ),
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            "strict": True,
        }


def validate_search_arguments(raw: Mapping[str, Any]) -> SearchDocsArguments:
    """Turn an untrusted argument map into a typed, bounded argument set.

    Every rejection is explicit and typed. Missing, extra, wrongly-typed and
    out-of-bounds arguments each fail for their own stated reason rather than
    collapsing into one, so a recorded rejection says what the model actually
    got wrong.

    Raises:
        ToolRejected: on anything that does not satisfy the contract.
    """
    if not isinstance(raw, Mapping):
        raise ToolRejected(RejectionReason.MALFORMED_ARGUMENTS, "arguments were not an object")

    unexpected = sorted(set(raw) - {"query"})
    if unexpected:
        raise ToolRejected(
            RejectionReason.UNEXPECTED_ARGUMENT,
            f"unexpected argument(s): {', '.join(unexpected)}",
        )
    if "query" not in raw:
        raise ToolRejected(RejectionReason.MISSING_ARGUMENT, "missing required argument: query")

    query = raw["query"]
    if not isinstance(query, str):
        raise ToolRejected(RejectionReason.WRONG_ARGUMENT_TYPE, "query must be a string")

    stripped = query.strip()
    if len(stripped) < MIN_QUERY_LENGTH:
        raise ToolRejected(RejectionReason.ARGUMENT_TOO_SHORT, "query is too short")
    if len(stripped) > MAX_QUERY_LENGTH:
        raise ToolRejected(RejectionReason.ARGUMENT_TOO_LONG, "query is too long")

    return SearchDocsArguments(query=stripped)


class LabToolRegistry:
    """An immutable collection of tools. The authority on names and risk."""

    def __init__(self, tools: list[LabTool]) -> None:
        names = [tool.name for tool in tools]
        if len(set(names)) != len(names):
            raise ValueError("Tool names must be unique within the registry.")
        for tool in tools:
            if tool.risk is not ToolRiskLevel.READ_ONLY:
                # Phase 19.1b is a read-only slice. A state-changing tool is
                # refused at construction rather than guarded at call time,
                # because the approval machinery that would make one safe lives
                # in the product and has not been ported here.
                raise ValueError(f"{tool.name}: Phase 19.1b registers read-only tools only.")
        self._by_name = {tool.name: tool for tool in tools}

    def get(self, name: str) -> LabTool | None:
        return self._by_name.get(name)

    def risk_of(self, name: str) -> ToolRiskLevel | None:
        tool = self._by_name.get(name)
        return tool.risk if tool else None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def function_schemas(self) -> list[dict[str, Any]]:
        """What is sent to Foundry: the allow-listed tools, and nothing else."""
        return [
            tool.function_schema()
            for tool in sorted(self._by_name.values(), key=lambda t: t.name)
            if tool.allowed
        ]


__all__ = [
    "MAX_QUERY_LENGTH",
    "MAX_TOOL_CALLS",
    "MIN_QUERY_LENGTH",
    "LabTool",
    "LabToolRegistry",
    "RejectionReason",
    "SearchDocsArguments",
    "ToolRejected",
    "ToolRiskLevel",
    "validate_search_arguments",
]
