"""The Foundry runtime boundary, and a deterministic fake.

Everything above this protocol — validation, dispatch, bounding, recording — is
application-owned and testable with no network, no Azure and no credential. The
real adapter lives in `foundry.py` and is imported only when a live run asks
for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProposedCall:
    """A function call Foundry proposed. UNTRUSTED until validated."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    """One response from the managed runtime.

    Either it proposes calls, or it carries final text. The identifiers are what
    make a turn auditable across the boundary: the conversation groups the turn,
    the response id names this step within it.
    """

    response_id: str
    conversation_id: str
    proposed_calls: tuple[ProposedCall, ...] = ()
    output_text: str = ""
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.proposed_calls)


@runtime_checkable
class AgentRuntime(Protocol):
    """What a managed agent runtime must provide for this lab."""

    def ensure_agent(self, tool_schemas: list[dict[str, Any]], instructions: str) -> str:
        """Create or update the agent definition. Returns its version identifier."""
        ...

    def start_conversation(self) -> str:
        """Open a conversation and return its identifier."""
        ...

    def respond(self, conversation_id: str, user_input: str) -> RuntimeResponse:
        """Send a user turn."""
        ...

    def submit_tool_outputs(
        self, conversation_id: str, outputs: list[tuple[str, str]]
    ) -> RuntimeResponse:
        """Return (call_id, output) pairs and get the next response."""
        ...


@dataclass
class FakeAgentRuntime:
    """A scripted runtime. Same request in, same response out, always.

    It is also the adversary: the scripted responses deliberately include the
    ways a managed runtime can misbehave — proposing an unregistered tool,
    omitting a required argument, inventing an extra one — because those are
    what the validation layer exists to catch.
    """

    scripted: list[RuntimeResponse] = field(default_factory=list)
    conversation_id: str = "conv-fake-0001"
    agent_version: str = "1"
    ensure_calls: list[tuple[tuple[str, ...], str]] = field(default_factory=list)
    submitted: list[list[tuple[str, str]]] = field(default_factory=list)
    _index: int = 0

    def ensure_agent(self, tool_schemas: list[dict[str, Any]], instructions: str) -> str:
        self.ensure_calls.append((tuple(s["name"] for s in tool_schemas), instructions))
        return self.agent_version

    def start_conversation(self) -> str:
        return self.conversation_id

    def _next(self) -> RuntimeResponse:
        if self._index >= len(self.scripted):
            raise AssertionError("the fake runtime ran out of scripted responses")
        response = self.scripted[self._index]
        self._index += 1
        return response

    def respond(self, conversation_id: str, user_input: str) -> RuntimeResponse:
        return self._next()

    def submit_tool_outputs(
        self, conversation_id: str, outputs: list[tuple[str, str]]
    ) -> RuntimeResponse:
        self.submitted.append(list(outputs))
        return self._next()


__all__ = ["AgentRuntime", "FakeAgentRuntime", "ProposedCall", "RuntimeResponse"]
