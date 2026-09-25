"""The model-facing tool catalogue: derived, deterministic, and free of authority.

WHY THIS FILE EXISTS
--------------------
A second live defect, found only after the wire-contract fix let /v1/agent reach
the model at all. Asked about production Foundry capacity, the model chose the
RIGHT tool and then supplied `component_name`, because the catalogue told it the
tool's name and purpose but never its arguments. `ComponentLookupInput` declares
`component`, so policy denied the proposal for invalid arguments and nothing ran.

The controls behaved correctly; the contract was simply incomplete. Argument
names are server-owned metadata, not a security boundary — withholding them only
made the model guess. Risk classification is a different matter and is still
withheld, which these tests hold apart.
"""

from __future__ import annotations

import dataclasses

import pytest

from platform_engineering_assistant.agent.domain import (
    AgentOutcomeKind,
    AgentRequest,
    DenialReason,
    PolicyDecision,
    ToolExecutionStatus,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.orchestrator import render_catalogue
from platform_engineering_assistant.agent.registry import (
    ArgumentSpec,
    ToolRegistry,
    argument_specs,
    build_registry,
)
from platform_engineering_assistant.retrieval.index import build_index
from tests.agent_fakes import DEFAULT_CHUNKS, agent_service, propose_change, use_tool
from tests.generation_fakes import CONFIG

QUESTION = "What does the documentation say about the sandbox environment?"


@pytest.fixture
def registry() -> ToolRegistry:
    return build_registry(build_index(DEFAULT_CHUNKS, CONFIG))


# --- 1. every required argument name is actually named ----------------------


def test_every_registered_tools_required_arguments_appear_in_the_catalogue(
    registry: ToolRegistry,
) -> None:
    """THE regression. The model cannot supply what it was never told.

    Checked against each tool's OWN input model rather than a hand-written list,
    so a tool gaining a required argument fails here until the catalogue names
    it — which is the drift that caused the outage.
    """
    rendered = render_catalogue(registry)
    for definition in registry.definitions:
        for name, field in definition.tool.input_model.model_fields.items():
            if field.is_required():
                assert name in rendered, f"{definition.name} never names required argument '{name}'"


def test_the_catalogue_names_the_argument_the_model_previously_guessed(
    registry: ToolRegistry,
) -> None:
    """The exact live failure: `component_name` guessed, `component` required."""
    rendered = render_catalogue(registry)
    assert "component: string, required" in rendered
    assert "component_name" not in rendered


def test_optional_arguments_are_marked_optional(registry: ToolRegistry) -> None:
    rendered = render_catalogue(registry)
    assert "environment: string, optional" in rendered


def test_specs_are_derived_from_the_input_model_not_duplicated() -> None:
    """A hand-maintained list would eventually disagree with what validates."""
    from platform_engineering_assistant.agent.tools import ComponentLookupInput

    specs = argument_specs(ComponentLookupInput)
    assert [s.name for s in specs] == list(ComponentLookupInput.model_fields)
    by_name = {s.name: s for s in specs}
    assert by_name["component"].required is True
    assert by_name["environment"].required is False
    assert by_name["component"].type == "string"


# --- 2. determinism ----------------------------------------------------------


def test_the_catalogue_is_deterministic(registry: ToolRegistry) -> None:
    """Same registry in, byte-identical catalogue out, every time."""
    assert len({render_catalogue(registry) for _ in range(25)}) == 1


def test_two_registries_over_the_same_tools_render_identically() -> None:
    first = build_registry(build_index(DEFAULT_CHUNKS, CONFIG))
    second = build_registry(build_index(DEFAULT_CHUNKS, CONFIG))
    assert render_catalogue(first) == render_catalogue(second)


def test_argument_order_follows_declaration_order() -> None:
    from platform_engineering_assistant.agent.tools import ProposeChangeInput

    assert [s.name for s in argument_specs(ProposeChangeInput)] == [
        "title",
        "rationale",
        "environment",
    ]


# --- 3. nothing else leaks ---------------------------------------------------


def test_an_argument_spec_exposes_exactly_four_fields() -> None:
    """No constraints, no defaults, no validators, no Pydantic internals."""
    assert {f.name for f in dataclasses.fields(ArgumentSpec)} == {
        "name",
        "type",
        "required",
        "description",
    }


@pytest.mark.parametrize(
    "leak",
    [
        "min_length",
        "max_length",
        "minLength",
        "maxLength",
        "default_factory",
        "annotation",
        "json_schema",
        "$defs",
        "anyOf",
        "additionalProperties",
        "FieldInfo",
        "PydanticUndefined",
        "model_config",
    ],
)
def test_no_pydantic_or_schema_metadata_reaches_the_model(
    registry: ToolRegistry, leak: str
) -> None:
    assert leak not in render_catalogue(registry)


def test_the_catalogue_names_no_module_path_or_class(registry: ToolRegistry) -> None:
    """Implementation detail is not the model's business either."""
    rendered = render_catalogue(registry)
    for leak in ("platform_engineering_assistant", "BM25Index", "SearchDocsInput", "object at 0x"):
        assert leak not in rendered


# --- 4. risk remains registry-owned -----------------------------------------


def test_the_catalogue_still_discloses_no_risk(registry: ToolRegistry) -> None:
    rendered = render_catalogue(registry).lower()
    assert "read_only" not in rendered
    assert "state_changing" not in rendered


def test_risk_still_comes_from_the_registry_alone(registry: ToolRegistry) -> None:
    """Naming arguments changed what the model KNOWS, not what it may assert."""
    assert registry.risk_of("propose_change_request") is ToolRiskLevel.STATE_CHANGING
    assert registry.risk_of("search_platform_docs") is ToolRiskLevel.READ_ONLY
    for entry in registry.catalogue():
        assert "risk" not in {argument.name for argument in entry.arguments}


def test_a_model_claimed_risk_is_still_ignored() -> None:
    """The claim is recorded; the registry decides."""
    turn = agent_service(propose_change(claimed_risk=ToolRiskLevel.READ_ONLY)).run(
        AgentRequest(question="Please raise the sandbox capacity.")
    )
    assert turn.response.tool_risk_level is ToolRiskLevel.STATE_CHANGING
    assert turn.response.policy_decision is PolicyDecision.REQUIRE_APPROVAL
    assert turn.telemetry.risk_claim_mismatch is True


# --- 5, 6, 7. the behaviour the catalogue is supposed to enable --------------


def test_an_incorrect_argument_name_is_still_denied() -> None:
    """The catalogue helps the model; it does not relax validation.

    This is the live failure reproduced offline: a correct tool choice with a
    wrong argument name must still be denied before anything runs.
    """
    turn = agent_service(
        use_tool("lookup_platform_component", {"component_name": "foundry", "environment": "prod"})
    ).run(AgentRequest(question=QUESTION))
    assert turn.response.outcome is AgentOutcomeKind.DENIED
    assert turn.response.denial_reason is DenialReason.INVALID_ARGUMENTS
    assert turn.response.tool_execution_status is ToolExecutionStatus.NOT_EXECUTED


def test_a_correct_argument_name_reaches_execution() -> None:
    """The other half: the name the catalogue advertises must actually work."""
    turn = agent_service(
        use_tool("lookup_platform_component", {"component": "sandbox", "environment": "sandbox"})
    ).run(AgentRequest(question=QUESTION))
    assert turn.response.policy_decision is PolicyDecision.ALLOW
    assert turn.response.denial_reason is None
    assert turn.response.tool_execution_status is ToolExecutionStatus.SUCCEEDED
    assert turn.response.tool_iterations >= 1


def test_a_state_changing_proposal_still_requires_approval() -> None:
    """Naming arguments must not have opened a path around the human gate."""
    turn = agent_service(propose_change()).run(
        AgentRequest(question="Please raise the sandbox capacity.")
    )
    assert turn.response.outcome is AgentOutcomeKind.APPROVAL_REQUIRED
    assert turn.response.tool_risk_level is ToolRiskLevel.STATE_CHANGING
    assert turn.response.policy_decision is PolicyDecision.REQUIRE_APPROVAL
    assert turn.response.tool_execution_status is ToolExecutionStatus.NOT_EXECUTED
    assert turn.response.approval_id is not None
