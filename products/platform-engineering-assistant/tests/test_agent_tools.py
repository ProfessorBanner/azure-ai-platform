"""The typed tool contract, and the scope rule it enforces in code."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from platform_engineering_assistant.agent.domain import ToolRiskLevel
from platform_engineering_assistant.agent.tools import (
    KNOWN_ENVIRONMENTS,
    ComponentLookupInput,
    LookupPlatformComponentTool,
    ProposeChangeInput,
    ProposeChangeRequestTool,
    SearchDocsInput,
    SearchPlatformDocsTool,
    _environment_of,
    parse_or_none,
)
from platform_engineering_assistant.retrieval.index import build_index
from tests.generation_fakes import CONFIG, chunk

SANDBOX = chunk(
    "adr-0006::capacity::0::0",
    "The sandbox Foundry deployment is provisioned at capacity 10.",
    doc_id="adr-0006",
    doc_path="docs/adr/0006-x.md",
)
UNSCOPED = chunk(
    "adr-0006::model::0::0",
    "The deployment uses a Standard SKU and a pinned model version.",
    doc_id="adr-0006",
    doc_path="docs/adr/0006-x.md",
)
COMPARISON = chunk(
    "adr-0005::envs::0::0",
    "The platform has sandbox, dev, stg and prod environments.",
    doc_id="adr-0005",
    doc_path="docs/adr/0005-x.md",
)

INDEX = build_index([SANDBOX, UNSCOPED, COMPARISON], CONFIG)


# --- typed contract ----------------------------------------------------------


def test_tool_inputs_are_closed() -> None:
    with pytest.raises(ValidationError):
        SearchDocsInput(query="a valid query", extra="smuggled")  # type: ignore[call-arg]


def test_arguments_that_do_not_fit_yield_none_rather_than_a_coerced_default() -> None:
    tool = SearchPlatformDocsTool(INDEX)
    assert parse_or_none(tool, {"query": "terraform state"}) is not None
    assert parse_or_none(tool, {"wrong_key": "x"}) is None
    assert parse_or_none(tool, {"query": "ab"}) is None  # below min_length


def test_every_tool_declares_a_server_owned_risk() -> None:
    assert SearchPlatformDocsTool(INDEX).risk is ToolRiskLevel.READ_ONLY
    assert LookupPlatformComponentTool(INDEX).risk is ToolRiskLevel.READ_ONLY
    assert ProposeChangeRequestTool().risk is ToolRiskLevel.STATE_CHANGING


# --- scope detection ---------------------------------------------------------


def test_a_chunk_naming_one_environment_is_scoped_to_it() -> None:
    assert _environment_of(SANDBOX) == "sandbox"


def test_a_chunk_naming_no_environment_has_no_scope() -> None:
    """None means 'the source made no environment claim', so none may be built on it."""
    assert _environment_of(UNSCOPED) is None


def test_a_chunk_comparing_all_environments_is_scoped_to_none_of_them() -> None:
    """Evidence about four environments is not evidence about any one of them."""
    assert _environment_of(COMPARISON) is None


def test_environment_matching_respects_word_boundaries() -> None:
    """'dev' must never fire on 'developer' or 'device'."""
    assert _environment_of(chunk("x::y::0::0", "The developer device was provisioned.")) is None


# --- the scope rule ----------------------------------------------------------


def test_search_attaches_scope_to_every_evidence_item() -> None:
    tool = SearchPlatformDocsTool(INDEX)
    output = tool.run(SearchDocsInput(query="sandbox capacity deployment"))
    assert output.evidence
    for item in output.evidence:
        assert item.scope.authority
        assert item.scope.source_doc_ids


def test_a_lookup_for_an_environment_with_no_evidence_refuses_to_substitute() -> None:
    """The failure being designed out: one environment's evidence answering
    a question about another, carrying a real citation while doing it."""
    tool = LookupPlatformComponentTool(INDEX)
    output = tool.run(ComponentLookupInput(component="Foundry capacity", environment="prod"))

    assert output.found is False
    assert output.unsupported_environment is True
    assert output.evidence == ()
    assert output.environment_requested == "prod"


def test_the_refusal_names_the_environments_that_do_exist() -> None:
    """A caller learns what is available rather than guessing."""
    tool = LookupPlatformComponentTool(INDEX)
    output = tool.run(ComponentLookupInput(component="Foundry capacity", environment="prod"))
    assert "sandbox" in output.environments_available


def test_a_lookup_for_a_supported_environment_returns_only_that_scope() -> None:
    tool = LookupPlatformComponentTool(INDEX)
    output = tool.run(ComponentLookupInput(component="Foundry capacity", environment="sandbox"))
    assert output.found is True
    assert output.evidence
    for item in output.evidence:
        assert item.scope.environment == "sandbox"


def test_an_unscoped_lookup_returns_evidence_without_claiming_an_environment() -> None:
    tool = LookupPlatformComponentTool(INDEX)
    output = tool.run(ComponentLookupInput(component="deployment"))
    assert output.environment_requested is None
    assert output.found is True


def test_an_unknown_environment_is_rejected_rather_than_guessed() -> None:
    tool = LookupPlatformComponentTool(INDEX)
    output = tool.run(ComponentLookupInput(component="capacity", environment="preprod"))
    assert output.found is False
    assert output.unsupported_environment is True
    assert output.environments_available == KNOWN_ENVIRONMENTS


def test_the_scoped_chunk_helper_agrees_with_the_tool_output() -> None:
    """Grounding must see exactly the chunks the tool said it had."""
    tool = LookupPlatformComponentTool(INDEX)
    payload = ComponentLookupInput(component="Foundry capacity", environment="prod")
    assert tool.run(payload).found is False
    assert tool.chunks_for(payload) == []


# --- the state-changing tool --------------------------------------------------


def test_the_proposal_tool_performs_no_mutation_and_refuses_to_run() -> None:
    from platform_engineering_assistant.agent.tools import ToolError

    with pytest.raises(ToolError):
        ProposeChangeRequestTool().run(
            ProposeChangeInput(title="a proposed title", rationale="a sufficient rationale here")
        )


def test_the_approval_summary_uses_only_validated_typed_input() -> None:
    summary = ProposeChangeRequestTool.summarise(
        ProposeChangeInput(
            title="Raise capacity", rationale="Throttled during evaluation", environment="sandbox"
        )
    )
    assert summary == "Proposed change [sandbox]: Raise capacity"


def test_the_proposal_tool_rejects_an_implausibly_thin_rationale() -> None:
    with pytest.raises(ValidationError):
        ProposeChangeInput(title="a title here", rationale="short")


# --- retrieval reuse ----------------------------------------------------------


def test_search_reuses_the_servers_retrieval_configuration() -> None:
    """No widened top-k, no lowered floor: an agent cannot enlarge the evidence
    set until an answer appears."""
    tool = SearchPlatformDocsTool(INDEX)
    output = tool.run(SearchDocsInput(query="sandbox capacity"))
    assert output.result_count <= CONFIG.top_k


def test_an_empty_result_is_reported_as_empty_not_as_an_answer() -> None:
    tool = SearchPlatformDocsTool(INDEX)
    output = tool.run(SearchDocsInput(query="zzzz unrelated vocabulary zzzz"))
    assert output.empty is True
    assert output.evidence == ()
