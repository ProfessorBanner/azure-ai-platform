"""The agent decision WIRE CONTRACT: what is sent to the provider, and its bounds.

WHY THIS FILE EXISTS
--------------------
A live defect: /v1/answers worked while /v1/agent failed every time with a 400
from the Responses API. The cause was not the agent architecture but the shape
of one field. `tool_arguments: dict[str, str]` renders as
`additionalProperties: {"type": "string"}`, and its bound as `maxProperties` —
neither of which strict Structured Outputs permits. The upstream error was:

    Invalid schema for response_format 'AgentDecision':
    In context=('properties', 'tool_arguments'), 'maxProperties' is not permitted.

Nothing offline caught it, because the deterministic fake never builds a JSON
schema. These tests close that gap: they assert properties OF THE SCHEMA rather
than of a response, so the schema cannot regress without a live call.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from platform_engineering_assistant.agent.domain import (
    MAX_ARGUMENT_KEY_LENGTH,
    MAX_ARGUMENT_VALUE_LENGTH,
    MAX_TOOL_ARGUMENTS,
    AgentDecision,
    DecisionKind,
    ToolArgument,
)


def strict_schema() -> dict[str, Any]:
    """The exact schema the OpenAI SDK sends for AgentDecision.

    Built with the SDK's own transform rather than `model_json_schema()`, so the
    test asserts against what actually goes on the wire.
    """
    from openai.lib._pydantic import to_strict_json_schema

    schema: dict[str, Any] = to_strict_json_schema(AgentDecision)
    return schema


def walk(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every (path, node) pair in the schema, so assertions can be exhaustive."""
    found = [(path, node)]
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(walk(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(walk(value, f"{path}/{index}"))
    return found


# --- 1. THE REGRESSION TEST: no dynamic object shape anywhere ---------------


def test_the_schema_declares_no_arbitrary_additional_properties() -> None:
    """THE regression. `additionalProperties` may only ever be `false`.

    An object-valued `additionalProperties` is a map of arbitrary keys, which
    strict Structured Outputs cannot express and the provider rejects. Checked
    over EVERY node rather than over the one field that caused the outage, so a
    dynamic map added anywhere in the decision contract fails here.
    """
    offenders = [
        path
        for path, node in walk(strict_schema())
        if isinstance(node, dict) and node.get("additionalProperties") not in (None, False)
    ]
    assert not offenders, f"schema declares arbitrary additionalProperties at: {offenders}"


@pytest.mark.parametrize("keyword", ["maxProperties", "minProperties", "maxItems", "minItems"])
def test_the_schema_carries_no_bound_keyword_the_provider_rejects(keyword: str) -> None:
    """Bounds belong in validators, not in the emitted schema.

    `maxProperties` is the keyword that actually caused the 400. The others are
    included because they are the same class of mistake — a Field constraint
    that renders as a schema keyword — and would fail the same way.
    """
    offenders = [path for path, node in walk(strict_schema()) if path.endswith(f"/{keyword}")]
    assert not offenders, f"schema carries '{keyword}' at: {offenders}"


def test_every_object_in_the_schema_closes_itself() -> None:
    """Strict mode requires additionalProperties: false on every object."""
    for path, node in walk(strict_schema()):
        if isinstance(node, dict) and node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path} is not closed"


def test_tool_arguments_is_an_array_of_declared_pairs() -> None:
    schema = strict_schema()
    field = schema["properties"]["tool_arguments"]
    assert field["type"] == "array"
    assert field["items"]["$ref"] == "#/$defs/ToolArgument"
    pair = schema["$defs"]["ToolArgument"]
    assert sorted(pair["properties"]) == ["name", "value"]
    assert sorted(pair["required"]) == ["name", "value"]
    assert pair["additionalProperties"] is False


def test_the_detector_catches_the_shape_that_caused_the_outage() -> None:
    """Guards the guard.

    The assertions above pass trivially if `walk` misses nodes or if the SDK
    transform stops emitting what we think it does. This rebuilds the ORIGINAL
    defective shape — a `dict[str, str]` with a bound — and proves both
    detectors fire on it. If this test ever fails, the ones above are asleep.
    """
    from openai.lib._pydantic import to_strict_json_schema
    from pydantic import BaseModel, ConfigDict, Field

    class DefectiveDecision(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        kind: DecisionKind
        tool_arguments: dict[str, str] = Field(default_factory=dict, max_length=8)

    defective = to_strict_json_schema(DefectiveDecision)

    dynamic_maps = [
        path
        for path, node in walk(defective)
        if isinstance(node, dict) and node.get("additionalProperties") not in (None, False)
    ]
    bounds = [path for path, node in walk(defective) if path.endswith("/maxProperties")]

    assert dynamic_maps, "the additionalProperties detector would not have caught the outage"
    assert bounds, "the maxProperties detector would not have caught the outage"


# --- 2. the encoding still means what the mapping meant ---------------------


def test_arguments_converts_pairs_to_the_mapping_the_tools_expect() -> None:
    decision = AgentDecision(
        kind=DecisionKind.USE_TOOL,
        tool_name="search_platform_docs",
        tool_arguments=[ToolArgument(name="query", value="terraform state")],
    )
    assert decision.arguments == {"query": "terraform state"}


def test_a_decision_with_no_arguments_converts_to_an_empty_mapping() -> None:
    assert AgentDecision(kind=DecisionKind.NO_TOOL).arguments == {}


def test_the_conversion_preserves_every_pair() -> None:
    pairs = [ToolArgument(name=f"k{i}", value=f"v{i}") for i in range(MAX_TOOL_ARGUMENTS)]
    decision = AgentDecision(
        kind=DecisionKind.USE_TOOL, tool_name="search_platform_docs", tool_arguments=pairs
    )
    assert decision.arguments == {f"k{i}": f"v{i}" for i in range(MAX_TOOL_ARGUMENTS)}


# --- 3. the bounds are real, now that the schema does not carry them --------


def test_a_repeated_argument_name_is_rejected() -> None:
    """A list can say the same thing twice; a mapping cannot.

    Silently keeping the last value would let a proposal assert two different
    values while only one of them was validated, executed and audited. The
    duplicate is refused instead, which is what makes `arguments` lossless.
    """
    with pytest.raises(ValidationError, match="must not repeat a name"):
        AgentDecision(
            kind=DecisionKind.USE_TOOL,
            tool_name="search_platform_docs",
            tool_arguments=[
                ToolArgument(name="query", value="first"),
                ToolArgument(name="query", value="second"),
            ],
        )


def test_too_many_arguments_are_rejected_by_the_validator() -> None:
    """The bound moved out of the schema; it must not have been lost."""
    with pytest.raises(ValidationError, match="too many tool arguments"):
        AgentDecision(
            kind=DecisionKind.USE_TOOL,
            tool_name="search_platform_docs",
            tool_arguments=[
                ToolArgument(name=f"k{i}", value="v") for i in range(MAX_TOOL_ARGUMENTS + 1)
            ],
        )


def test_an_over_long_argument_name_is_rejected() -> None:
    with pytest.raises(ValidationError, match="name is too long"):
        AgentDecision(
            kind=DecisionKind.USE_TOOL,
            tool_name="search_platform_docs",
            tool_arguments=[ToolArgument(name="k" * (MAX_ARGUMENT_KEY_LENGTH + 1), value="v")],
        )


def test_an_over_long_argument_value_is_rejected() -> None:
    with pytest.raises(ValidationError, match="value is too long"):
        AgentDecision(
            kind=DecisionKind.USE_TOOL,
            tool_name="search_platform_docs",
            tool_arguments=[ToolArgument(name="k", value="v" * (MAX_ARGUMENT_VALUE_LENGTH + 1))],
        )


def test_an_unnamed_argument_is_rejected() -> None:
    """An empty name would collapse into a mapping key nothing can validate."""
    with pytest.raises(ValidationError, match="must be named"):
        AgentDecision(
            kind=DecisionKind.USE_TOOL,
            tool_name="search_platform_docs",
            tool_arguments=[ToolArgument(name="   ", value="v")],
        )


# --- 4. the contract is still closed and still untrusted --------------------


def test_a_pair_carrying_an_invented_field_never_parses() -> None:
    with pytest.raises(ValidationError):
        ToolArgument(name="k", value="v", risk_level="read_only")  # type: ignore[call-arg]


def test_a_pair_is_frozen_once_built() -> None:
    pair = ToolArgument(name="k", value="v")
    with pytest.raises(ValidationError):
        pair.name = "other"


def test_the_decision_is_still_closed_to_invented_fields() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(kind=DecisionKind.NO_TOOL, authorised=True)  # type: ignore[call-arg]


def test_the_model_still_cannot_supply_authority() -> None:
    """The encoding changed; what a proposal may assert did not."""
    declared = set(AgentDecision.model_fields)
    for forbidden in ("authorised", "approved", "policy_decision", "risk_level", "allowed"):
        assert forbidden not in declared
