"""The tool registry: the single source of truth for what a tool IS.

WHY A REGISTRY RATHER THAN A LIST OF CALLABLES
----------------------------------------------
The registry owns the metadata that decides authorisation: the tool's name, its
risk classification and whether it is on the allow-list. That metadata is built
in server-side code at startup and is immutable afterwards.

Nothing a model emits can add a tool, rename one, or change one's risk. A
proposal naming an unregistered tool has nowhere to resolve to, which is why an
unknown tool is a DENIAL rather than an attempt.

THE ALLOW-LIST IS SEPARATE FROM REGISTRATION
--------------------------------------------
A tool can be registered and not allowed. The distinction matters: registration
is "this tool exists and here is its risk", the allow-list is "this deployment
permits it". Collapsing them would mean disabling a tool required deleting its
definition, and a deleted definition takes its risk classification with it.
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass

from pydantic import BaseModel

from platform_engineering_assistant.agent.domain import ToolRiskLevel
from platform_engineering_assistant.agent.tools import (
    LookupPlatformComponentTool,
    ProposeChangeRequestTool,
    SearchPlatformDocsTool,
    Tool,
    ToolInput,
    ToolOutput,
)
from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.retrieval.index import BM25Index

AnyTool = Tool[ToolInput, ToolOutput]

# The only annotations the catalogue will name. Anything else is described as a
# string, because the wire contract carries every argument as a string anyway
# and inventing a richer vocabulary would describe a precision the model cannot
# act on.
_SIMPLE_TYPES: dict[object, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """One argument, as the model is told about it.

    Deliberately four fields. A name, a simple type and whether it is required
    are what a model needs to produce a valid proposal; a description is offered
    when the input model already carries a safe one. Nothing else is exposed —
    no constraints, no defaults, no validators, no Pydantic internals — because
    everything beyond this is either useless to the model or a detail of how the
    server enforces its own rules.
    """

    name: str
    type: str
    required: bool
    description: str = ""


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """What the model is TOLD about one tool."""

    name: str
    description: str
    arguments: tuple[ArgumentSpec, ...]


def argument_specs(input_model: type[BaseModel]) -> tuple[ArgumentSpec, ...]:
    """Derive the argument contract from a tool's own trusted input model.

    DERIVED, NEVER DUPLICATED. The names the model is shown come from the same
    class that later validates them, so the catalogue cannot drift from the
    contract it describes. A hand-maintained list would eventually disagree with
    the model it documents, and the failure would look exactly like the one this
    exists to prevent: a plausible proposal denied for invalid arguments.

    Declaration order is preserved, which makes the output deterministic without
    needing to sort — and keeps related arguments adjacent for the reader.
    """
    specs: list[ArgumentSpec] = []
    for name, field in input_model.model_fields.items():
        annotation = field.annotation
        origin = typing.get_origin(annotation)
        if origin is typing.Union or origin is types.UnionType:
            candidates = [a for a in typing.get_args(annotation) if a is not type(None)]
            annotation = candidates[0] if len(candidates) == 1 else None
        specs.append(
            ArgumentSpec(
                name=name,
                type=_SIMPLE_TYPES.get(annotation, "string"),
                required=field.is_required(),
                description=field.description or "",
            )
        )
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Trusted metadata for one tool. Built server-side, never from model output."""

    name: str
    description: str
    risk: ToolRiskLevel
    tool: AnyTool
    allowed: bool = True

    def catalogue_entry(self) -> CatalogueEntry:
        """What the model is TOLD about this tool.

        Name, purpose and the ARGUMENT CONTRACT, derived from the tool's own
        input model. The risk level is still withheld: it is not the model's
        business, and showing it would invite an argument the policy layer does
        not participate in.

        Argument names are a different matter, and conflating the two was a real
        defect. Withholding them is not a security boundary — they are already
        server-owned metadata, and a model that cannot see them must GUESS,
        which live traffic showed it doing: it proposed `component_name` where
        the tool declares `component`, and a correct tool choice was denied for
        invalid arguments. Naming them costs nothing and removes a whole class
        of avoidable denial.
        """
        return CatalogueEntry(
            name=self.name,
            description=self.description,
            arguments=argument_specs(self.tool.input_model),
        )


class ToolRegistry:
    """An immutable collection of tool definitions."""

    def __init__(self, definitions: list[ToolDefinition]) -> None:
        names = [definition.name for definition in definitions]
        if len(set(names)) != len(names):
            raise ConfigurationError("Tool names must be unique within the registry.")
        self._by_name = {definition.name: definition for definition in definitions}

    def get(self, name: str) -> ToolDefinition | None:
        """The definition, or None when the name is not registered."""
        return self._by_name.get(name)

    def risk_of(self, name: str) -> ToolRiskLevel | None:
        """The AUTHORITATIVE risk classification for a tool name."""
        definition = self._by_name.get(name)
        return definition.risk if definition else None

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Every definition, in registration order. A copy, so callers cannot edit it."""
        return tuple(self._by_name.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def catalogue(self) -> list[CatalogueEntry]:
        """The allow-listed tools, as the model is shown them."""
        return [
            definition.catalogue_entry()
            for definition in sorted(self._by_name.values(), key=lambda d: d.name)
            if definition.allowed
        ]


def build_registry(index: BM25Index) -> ToolRegistry:
    """Construct the Phase 18.1 registry.

    Three tools: two read-only, one state-changing. The state-changing one
    performs no mutation and exists to prove the approval path.
    """
    search = SearchPlatformDocsTool(index)
    lookup = LookupPlatformComponentTool(index)
    propose = ProposeChangeRequestTool()

    return ToolRegistry(
        [
            ToolDefinition(
                name=search.name,
                description=search.description,
                risk=search.risk,
                tool=search,  # type: ignore[arg-type]
            ),
            ToolDefinition(
                name=lookup.name,
                description=lookup.description,
                risk=lookup.risk,
                tool=lookup,  # type: ignore[arg-type]
            ),
            ToolDefinition(
                name=propose.name,
                description=propose.description,
                risk=propose.risk,
                tool=propose,  # type: ignore[arg-type]
            ),
        ]
    )


__all__ = [
    "AnyTool",
    "ArgumentSpec",
    "CatalogueEntry",
    "ToolDefinition",
    "ToolRegistry",
    "argument_specs",
    "build_registry",
]
