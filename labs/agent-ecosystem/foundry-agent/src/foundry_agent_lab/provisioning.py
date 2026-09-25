"""Agent provisioning: a deliberate, separate step from serving a turn.

WHY VERSION CREATION IS NOT PART OF STARTUP
-------------------------------------------
`GovernedFoundryLoop.run` used to call `ensure_agent` on every turn. That is
convenient and wrong, for three reasons:

  * it makes a WRITE to the agent definition part of a read path, so a request
    that should only consume the platform can change it;
  * it creates a new immutable version per turn, so "which definition produced
    this answer" stops being answerable — the versions become noise;
  * it couples serving availability to the control plane. A definition endpoint
    having a bad day would take answering down with it.

So provisioning is explicit: someone runs it, it returns a version, and that
version is passed to the loop and recorded on every turn. Runtime routing then
consumes an EXISTING version and never writes.

IMMUTABILITY IS THE POINT
-------------------------
`agents.create_version` produces a new version rather than editing one, which is
the managed equivalent of the prompt version and content hash the Phase 18
product ships with every response. The version identifier is the thing that ties
an answer back to the instructions and tool contract that produced it, so it is
carried through the correlation record rather than looked up later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foundry_agent_lab.protocol import AgentRuntime


@dataclass(frozen=True, slots=True)
class ProvisionedAgent:
    """The definition a turn will run against. Recorded, never re-derived."""

    agent_name: str
    agent_version: str
    tool_names: tuple[str, ...]
    instructions_chars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "tool_names": list(self.tool_names),
            "instructions_chars": self.instructions_chars,
        }


def provision_agent(
    runtime: AgentRuntime,
    agent_name: str,
    tool_schemas: list[dict[str, Any]],
    instructions: str,
) -> ProvisionedAgent:
    """Create a new immutable agent version carrying this tool contract.

    The only call in this lab that writes to the agent definition. It is not
    reachable from the serving path.
    """
    version = runtime.ensure_agent(tool_schemas, instructions)
    return ProvisionedAgent(
        agent_name=agent_name,
        agent_version=version,
        tool_names=tuple(schema["name"] for schema in tool_schemas),
        # The instruction TEXT is not recorded: it is prompt data, and the
        # version identifier already ties a turn to it. The length is kept
        # because a silent change in size is worth noticing.
        instructions_chars=len(instructions),
    )


__all__ = ["ProvisionedAgent", "provision_agent"]
