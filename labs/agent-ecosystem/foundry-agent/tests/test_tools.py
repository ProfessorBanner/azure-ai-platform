"""The tool contract: server-owned risk, a strict schema, and validation."""

from __future__ import annotations

from typing import Any

import pytest

from foundry_agent_lab.registry import build_registry
from foundry_agent_lab.tools import (
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    LabTool,
    LabToolRegistry,
    RejectionReason,
    ToolRejected,
    ToolRiskLevel,
    validate_search_arguments,
)
from tests.fakes import recording_registry


def schema() -> dict[str, Any]:
    registry, _ = recording_registry()
    return registry.function_schemas()[0]


# --- risk stays server-owned -------------------------------------------------


def test_only_one_tool_is_registered_and_it_is_read_only() -> None:
    registry, _ = recording_registry()
    assert registry.names == ("search_platform_docs",)
    assert registry.risk_of("search_platform_docs") is ToolRiskLevel.READ_ONLY


def test_a_state_changing_tool_cannot_be_registered_at_all() -> None:
    """Refused at construction, not guarded at call time.

    The approval machinery that would make a state-changing tool safe lives in
    the product and has not been ported here, so the honest posture is that one
    cannot be registered.
    """
    with pytest.raises(ValueError, match="read-only"):
        LabToolRegistry(
            [
                LabTool(
                    name="propose_change",
                    description="x",
                    risk=ToolRiskLevel.STATE_CHANGING,
                    run=lambda a: "",
                )
            ]
        )


def test_risk_is_never_sent_to_foundry() -> None:
    rendered = repr(schema())
    assert "read_only" not in rendered
    assert "state_changing" not in rendered
    assert "risk" not in rendered


def test_risk_comes_from_the_registry_not_from_a_call() -> None:
    registry, _ = recording_registry()
    assert registry.risk_of("search_platform_docs") is ToolRiskLevel.READ_ONLY
    assert registry.risk_of("anything_else") is None


# --- the schema is strict, and carries no rejected keywords ------------------


def test_the_function_schema_is_strict_and_closed() -> None:
    s = schema()
    assert s["type"] == "function"
    assert s["strict"] is True
    parameters = s["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["query"]
    assert sorted(parameters["properties"]) == ["query"]


@pytest.mark.parametrize(
    "keyword", ["minLength", "maxLength", "maxProperties", "minProperties", "maxItems", "pattern"]
)
def test_the_schema_carries_no_keyword_that_broke_phase_18(keyword: str) -> None:
    """Bounds live in the validator. Phase 18 lost a day to exactly this."""
    assert keyword not in repr(schema())


def test_every_declared_property_is_required() -> None:
    """Strict mode requires it, and an optional property would be a silent gap."""
    parameters = schema()["parameters"]
    assert set(parameters["required"]) == set(parameters["properties"])


# --- validation rejects everything that is not the contract ------------------


def test_a_valid_argument_set_is_accepted_and_trimmed() -> None:
    assert validate_search_arguments({"query": "  terraform state  "}).query == "terraform state"


def test_a_missing_argument_is_rejected() -> None:
    with pytest.raises(ToolRejected) as e:
        validate_search_arguments({})
    assert e.value.reason is RejectionReason.MISSING_ARGUMENT


def test_an_extra_argument_is_rejected() -> None:
    """The Phase 18 failure mode: a plausible but invented argument."""
    with pytest.raises(ToolRejected) as e:
        validate_search_arguments({"query": "terraform state", "top_k": 500})
    assert e.value.reason is RejectionReason.UNEXPECTED_ARGUMENT


def test_a_wrongly_typed_argument_is_rejected() -> None:
    with pytest.raises(ToolRejected) as e:
        validate_search_arguments({"query": 7})
    assert e.value.reason is RejectionReason.WRONG_ARGUMENT_TYPE


def test_a_malformed_argument_object_is_rejected() -> None:
    with pytest.raises(ToolRejected) as e:
        validate_search_arguments(["query"])  # type: ignore[arg-type]
    assert e.value.reason is RejectionReason.MALFORMED_ARGUMENTS


def test_a_too_short_query_is_rejected() -> None:
    with pytest.raises(ToolRejected) as e:
        validate_search_arguments({"query": "x" * (MIN_QUERY_LENGTH - 1)})
    assert e.value.reason is RejectionReason.ARGUMENT_TOO_SHORT


def test_a_too_long_query_is_rejected() -> None:
    with pytest.raises(ToolRejected) as e:
        validate_search_arguments({"query": "x" * (MAX_QUERY_LENGTH + 1)})
    assert e.value.reason is RejectionReason.ARGUMENT_TOO_LONG


# --- the real registry -------------------------------------------------------


def test_the_shipped_registry_holds_exactly_one_read_only_tool() -> None:
    registry = build_registry()
    assert registry.names == ("search_platform_docs",)
    assert registry.risk_of("search_platform_docs") is ToolRiskLevel.READ_ONLY
    assert len(registry.function_schemas()) == 1
