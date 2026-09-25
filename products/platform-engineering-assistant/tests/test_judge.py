"""The semantic judge: schema, arithmetic, inputs and the rubric it is held to."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pydantic import ValidationError

from platform_engineering_assistant.domain import Citation
from platform_engineering_assistant.evaluation.judge import (
    JUDGE_PROMPT_FILE,
    JudgeRequest,
    JudgeVerdict,
    load_judge_prompt,
    render_cited_evidence,
)
from tests.generation_fakes import chunk

# --- schema -----------------------------------------------------------------


def test_the_verdict_is_closed_to_invented_fields() -> None:
    """An invented key must be a validation failure, not silently discarded."""
    with pytest.raises(ValidationError):
        JudgeVerdict(claims_total=1, claims_unsupported=0, answer_relevance=1.0, score=10)  # type: ignore[call-arg]


def test_unsupported_claims_cannot_exceed_total_claims() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        JudgeVerdict(claims_total=2, claims_unsupported=3, answer_relevance=1.0)


def test_relevance_is_bounded() -> None:
    with pytest.raises(ValidationError):
        JudgeVerdict(claims_total=1, claims_unsupported=0, answer_relevance=1.5)


def test_the_verdict_is_frozen() -> None:
    verdict = JudgeVerdict(claims_total=1, claims_unsupported=0, answer_relevance=1.0)
    with pytest.raises(ValidationError):
        verdict.claims_total = 5


# --- groundedness arithmetic -------------------------------------------------


def test_groundedness_is_the_supported_fraction() -> None:
    verdict = JudgeVerdict(claims_total=4, claims_unsupported=1, answer_relevance=1.0)
    assert verdict.groundedness == pytest.approx(0.75)


def test_a_refusal_asserting_nothing_is_fully_grounded() -> None:
    """Zero claims contradicts nothing, and contributes no claims to the rate either."""
    verdict = JudgeVerdict(claims_total=0, claims_unsupported=0, answer_relevance=1.0)
    assert verdict.groundedness == 1.0


def test_a_material_contradiction_scores_zero_regardless_of_the_claim_count() -> None:
    """A confident falsehood must not be diluted by the correct sentences around it."""
    verdict = JudgeVerdict(
        claims_total=10,
        claims_unsupported=1,
        answer_relevance=1.0,
        material_contradiction=True,
    )
    assert verdict.groundedness == 0.0


# --- what the judge is given --------------------------------------------------


def test_the_request_carries_no_expectation_field() -> None:
    """Enforced by the type, so no future caller can pass an answer key."""
    fields = {field.name for field in dataclasses.fields(JudgeRequest)}
    assert fields == {"rubric", "question", "response_text", "cited_evidence"}
    for forbidden in ("expected_disposition", "expected_doc_ids", "case_id", "score"):
        assert forbidden not in fields


def test_cited_evidence_renders_the_text_of_the_cited_chunks() -> None:
    chunks = {"a::b::0::0": chunk("a::b::0::0", "State lives in Azure Blob Storage.")}
    rendered = render_cited_evidence(
        [Citation(chunk_id="a::b::0::0", doc_id="adr-0002", doc_path="docs/x.md", score=1.0)],
        chunks.get,
    )
    assert "State lives in Azure Blob Storage." in rendered
    assert "[chunk_id: a::b::0::0]" in rendered


def test_an_unresolvable_chunk_is_marked_rather_than_dropped() -> None:
    """The judge must tell 'no evidence' apart from 'evidence that did not support it'."""
    rendered = render_cited_evidence(
        [Citation(chunk_id="gone::x::0::0", doc_id="adr-0002", doc_path="docs/x.md", score=1.0)],
        lambda _: None,
    )
    assert "could not be resolved" in rendered


def test_no_citations_renders_an_explicit_absence() -> None:
    assert render_cited_evidence([], lambda _: None) == "(the response cited no evidence)"


# --- the rubric ---------------------------------------------------------------


def test_the_judge_prompt_is_versioned_and_hashed() -> None:
    prompt = load_judge_prompt()
    assert prompt.version == "judge_v1"
    assert len(prompt.content_hash) == 64


@pytest.mark.parametrize(
    "requirement",
    [
        "claim",
        "cited evidence",
        "refusal",
        "contradiction",
        "not given the expected answer",
        "instruction",
        "correlated",
    ],
)
def test_the_rubric_states_each_required_rule(requirement: str) -> None:
    text = load_judge_prompt().text.lower()
    assert requirement.lower() in text, f"rubric does not state: {requirement}"


def test_the_rubric_records_the_correlated_bias_limitation() -> None:
    """The limitation travels with the rubric, not only with the README."""
    text = load_judge_prompt().text.lower()
    assert "same deployment" in text
    assert "not ground truth" in text or "estimate" in text
    assert "human review" in text


def test_the_rubric_file_is_named_by_the_module_constant(repo_root: Path) -> None:
    path = repo_root / "products" / "platform-engineering-assistant" / "prompts" / JUDGE_PROMPT_FILE
    assert path.is_file()


# --- no Azure ------------------------------------------------------------------


def test_importing_the_judge_module_does_not_import_any_azure_sdk() -> None:
    """The Azure client is built lazily, inside `from_config`.

    A module-level import would make the whole test suite depend on the SDK being
    installed and would put credential machinery one import away from every path.
    """
    import platform_engineering_assistant.evaluation.judge as module

    source = Path(module.__file__ or "").read_text()
    header = source.split("class AzureOpenAIJudge", 1)[0]
    assert "from azure.identity" not in header
    assert "from openai" not in header
