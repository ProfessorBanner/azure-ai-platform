"""The deterministic policy layer. Pure functions, exhaustively checked."""

from __future__ import annotations

import pytest

from platform_engineering_assistant.agent.domain import (
    AgentDecision,
    DecisionKind,
    DenialReason,
    PolicyDecision,
    ToolRiskLevel,
)
from platform_engineering_assistant.agent.policy import authorise
from platform_engineering_assistant.agent.registry import (
    ToolDefinition,
    ToolRegistry,
    build_registry,
)
from platform_engineering_assistant.agent.tools import ProposeChangeRequestTool
from platform_engineering_assistant.domain import RefusalReason
from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.retrieval.index import build_index
from tests.agent_fakes import DEFAULT_CHUNKS, no_tool, refuse, search, use_tool
from tests.generation_fakes import CONFIG


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return build_registry(build_index(DEFAULT_CHUNKS, CONFIG))


# --- the required rules ------------------------------------------------------


def test_read_only_is_allowed(registry: ToolRegistry) -> None:
    verdict = authorise(search(), registry)
    assert verdict.decision is PolicyDecision.ALLOW
    assert verdict.risk_level is ToolRiskLevel.READ_ONLY


def test_state_changing_requires_approval(registry: ToolRegistry) -> None:
    verdict = authorise(
        use_tool(
            "propose_change_request",
            {"title": "Raise sandbox capacity", "rationale": "Evaluation runs are throttled."},
        ),
        registry,
    )
    assert verdict.decision is PolicyDecision.REQUIRE_APPROVAL
    assert verdict.risk_level is ToolRiskLevel.STATE_CHANGING
    assert verdict.denial_reason is None


def test_an_unknown_tool_is_denied(registry: ToolRegistry) -> None:
    verdict = authorise(use_tool("delete_production_database"), registry)
    assert verdict.decision is PolicyDecision.DENY
    assert verdict.denial_reason is DenialReason.UNKNOWN_TOOL


def test_a_near_miss_name_is_not_generously_resolved(registry: ToolRegistry) -> None:
    """A near-miss resolved generously is how a model reaches a tool it was not offered."""
    for name in ("search_platform_doc", "Search_Platform_Docs", " search_platform_docs "):
        verdict = authorise(use_tool(name), registry)
        assert verdict.decision is PolicyDecision.DENY or name.strip() == "search_platform_docs"


def test_invalid_arguments_are_denied(registry: ToolRegistry) -> None:
    verdict = authorise(use_tool("search_platform_docs", {"quer": "typo"}), registry)
    assert verdict.decision is PolicyDecision.DENY
    assert verdict.denial_reason is DenialReason.INVALID_ARGUMENTS


def test_missing_required_arguments_are_denied(registry: ToolRegistry) -> None:
    verdict = authorise(use_tool("search_platform_docs", {}), registry)
    assert verdict.decision is PolicyDecision.DENY
    assert verdict.denial_reason is DenialReason.INVALID_ARGUMENTS


def test_a_tool_outside_the_allow_list_is_denied() -> None:
    """Registration and permission are separate: a tool can exist and be refused."""
    index = build_index(DEFAULT_CHUNKS, CONFIG)
    propose = ProposeChangeRequestTool()
    registry = ToolRegistry(
        [
            ToolDefinition(
                name=propose.name,
                description=propose.description,
                risk=propose.risk,
                tool=propose,  # type: ignore[arg-type]
                allowed=False,
            )
        ]
    )
    assert index is not None
    verdict = authorise(
        use_tool("propose_change_request", {"title": "x" * 6, "rationale": "y" * 12}), registry
    )
    assert verdict.decision is PolicyDecision.DENY
    assert verdict.denial_reason is DenialReason.NOT_ALLOW_LISTED


def test_no_tool_and_refuse_need_no_authorisation(registry: ToolRegistry) -> None:
    assert authorise(no_tool(), registry).decision is PolicyDecision.ALLOW
    assert authorise(refuse(), registry).decision is PolicyDecision.ALLOW


# --- the central invariant ---------------------------------------------------


def test_a_models_risk_claim_never_changes_the_verdict(registry: ToolRegistry) -> None:
    """THE invariant: risk is the registry's, not the model's.

    A model asserting that the state-changing tool is read-only must change
    nothing at all about the ruling.
    """
    honest = use_tool(
        "propose_change_request",
        {"title": "Raise capacity", "rationale": "Throttling during evaluation."},
    )
    lying = use_tool(
        "propose_change_request",
        {"title": "Raise capacity", "rationale": "Throttling during evaluation."},
        claimed_risk=ToolRiskLevel.READ_ONLY,
    )

    assert authorise(honest, registry) == authorise(lying, registry)
    assert authorise(lying, registry).decision is PolicyDecision.REQUIRE_APPROVAL
    assert authorise(lying, registry).risk_level is ToolRiskLevel.STATE_CHANGING


def test_a_claimed_risk_cannot_downgrade_a_denial(registry: ToolRegistry) -> None:
    verdict = authorise(use_tool("not_a_tool", claimed_risk=ToolRiskLevel.READ_ONLY), registry)
    assert verdict.decision is PolicyDecision.DENY


def test_the_registry_is_the_only_source_of_risk(registry: ToolRegistry) -> None:
    assert registry.risk_of("search_platform_docs") is ToolRiskLevel.READ_ONLY
    assert registry.risk_of("lookup_platform_component") is ToolRiskLevel.READ_ONLY
    assert registry.risk_of("propose_change_request") is ToolRiskLevel.STATE_CHANGING
    assert registry.risk_of("unregistered") is None


def _fields(entry: object) -> set[str]:
    import dataclasses

    return {f.name for f in dataclasses.fields(entry)}  # type: ignore[arg-type]


def test_the_catalogue_never_discloses_risk(registry: ToolRegistry) -> None:
    """Risk is not the model's business, and showing it would invite an argument."""
    for entry in registry.catalogue():
        assert set(vars(entry) if hasattr(entry, "__dict__") else _fields(entry)) == {
            "name",
            "description",
            "arguments",
        }
        rendered = f"{entry.name} {entry.description} {entry.arguments}".lower()
        assert "read_only" not in rendered
        assert "state_changing" not in rendered
        assert "risk" not in {argument.name for argument in entry.arguments}


# --- determinism and shape ---------------------------------------------------


def test_authorise_is_deterministic(registry: ToolRegistry) -> None:
    decision = search()
    assert len({authorise(decision, registry) for _ in range(25)}) == 1


def test_a_duplicate_tool_name_is_a_configuration_error() -> None:
    propose = ProposeChangeRequestTool()
    definition = ToolDefinition(
        name=propose.name,
        description=propose.description,
        risk=propose.risk,
        tool=propose,  # type: ignore[arg-type]
    )
    with pytest.raises(ConfigurationError, match="unique"):
        ToolRegistry([definition, definition])


def test_a_denial_always_states_a_reason(registry: ToolRegistry) -> None:
    for decision in (
        use_tool("unknown"),
        use_tool("search_platform_docs", {"wrong": "x"}),
    ):
        verdict = authorise(decision, registry)
        assert verdict.decision is PolicyDecision.DENY
        assert verdict.denial_reason is not None


def test_policy_detail_never_echoes_an_argument_value(registry: ToolRegistry) -> None:
    """Details name the rule; arguments are user-derived content and are logged."""
    secret = "a-very-distinctive-argument-value"
    verdict = authorise(use_tool("search_platform_docs", {"bogus": secret}), registry)
    assert secret not in verdict.detail


# --- schema-level rejection ---------------------------------------------------


def test_a_decision_carrying_an_invented_field_never_parses() -> None:
    """Closed schema: an invented field is how an unearned authorisation arrives."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentDecision.model_validate(
            {"kind": "use_tool", "tool_name": "search_platform_docs", "authorised": True}
        )


def test_a_use_tool_decision_must_name_a_tool() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must name a tool"):
        AgentDecision(kind=DecisionKind.USE_TOOL)


def test_a_refuse_decision_must_carry_a_reason() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="refusal_reason"):
        AgentDecision(kind=DecisionKind.REFUSE)


def test_a_no_tool_decision_must_not_name_a_tool() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must not name a tool"):
        AgentDecision(kind=DecisionKind.NO_TOOL, tool_name="search_platform_docs")


def test_a_refuse_decision_must_not_name_a_tool() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must not name a tool"):
        AgentDecision(
            kind=DecisionKind.REFUSE,
            refusal_reason=RefusalReason.OUT_OF_SCOPE,
            tool_name="propose_change_request",
        )
