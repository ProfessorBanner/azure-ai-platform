"""BM25 indexing and retrieval."""

from __future__ import annotations

from dataclasses import replace

import pytest

from platform_engineering_assistant.config import RetrievalConfig, load_retrieval_config
from platform_engineering_assistant.corpus import load_corpus, load_manifest
from platform_engineering_assistant.corpus.chunking import Chunk, chunk_corpus
from platform_engineering_assistant.retrieval.index import (
    BM25Index,
    RetrievalError,
    build_index,
)
from platform_engineering_assistant.retrieval.tokenize import tokenize

CONFIG = RetrievalConfig(
    version="retrieval_v1",
    top_k=5,
    minimum_score=0.0,
    bm25_k1=1.2,
    bm25_b=0.75,
    chunk_budget_chars=1200,
    title_weight=0.0,
    heading_weight=0.0,
)


def chunk(chunk_id: str, text: str, doc_id: str = "d", authority: str = "adr") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_path="docs/d.md",
        title="D",
        authority=authority,
        heading_path="H",
        section_occurrence=0,
        chunk_ordinal=0,
        content_sha256="0" * 64,
        text=text,
    )


CORPUS = [
    chunk("a::h::0::0", "Terraform remote state is stored in an Azure storage account."),
    chunk("b::h::0::0", "Databricks bundles are promoted from dev to stg to prod."),
    chunk("c::h::0::0", "The sandbox environment is disposable and not promoted."),
    chunk("d::h::0::0", "Unity Catalog isolates catalogs per environment."),
]


def index_of(chunks: list[Chunk], config: RetrievalConfig = CONFIG) -> BM25Index:
    return build_index(chunks, config)


# --- ranking ----------------------------------------------------------------


def test_exact_term_ranks_the_matching_chunk_first() -> None:
    results = index_of(CORPUS).search("Unity Catalog")
    assert results[0].chunk_id == "d::h::0::0"


def test_a_rare_term_outranks_a_common_one() -> None:
    """'promoted' appears twice; 'catalogs' once. The rarer term should win."""
    corpus = [
        chunk("x::h::0::0", "promoted promoted promoted"),
        chunk("y::h::0::0", "promoted catalogs"),
        chunk("z::h::0::0", "promoted"),
    ]
    results = index_of(corpus).search("promoted catalogs")
    assert results[0].chunk_id == "y::h::0::0"


def test_chunks_sharing_no_term_with_the_query_are_excluded() -> None:
    """Zero score is not weak evidence; it is no evidence."""
    results = index_of(CORPUS).search("Unity Catalog")
    assert all(result.score > 0 for result in results)
    assert "b::h::0::0" not in {result.chunk_id for result in results}


def test_out_of_scope_query_returns_nothing() -> None:
    assert index_of(CORPUS).search("photosynthesis chlorophyll") == []


# --- determinism ------------------------------------------------------------


def test_ties_are_broken_by_chunk_id_ascending() -> None:
    identical = [chunk(f"{letter}::h::0::0", "identical body text") for letter in "cabd"]
    results = index_of(identical).search("identical body text")
    assert [result.chunk_id for result in results] == sorted(r.chunk_id for r in results)


def test_repeated_queries_return_identical_results() -> None:
    index = index_of(CORPUS)
    first = [(r.chunk_id, r.score) for r in index.search("state promoted environment")]
    second = [(r.chunk_id, r.score) for r in index.search("state promoted environment")]
    assert first == second


def test_querying_does_not_mutate_the_index() -> None:
    index = index_of(CORPUS)
    before = (index.size, index._average_length, dict(index._document_frequencies))
    index.search("terraform state")
    index.search("databricks")
    after = (index.size, index._average_length, dict(index._document_frequencies))
    assert before == after


def test_result_order_does_not_depend_on_input_order() -> None:
    forward = index_of(list(CORPUS)).search("environment promoted")
    reversed_ = index_of(list(reversed(CORPUS))).search("environment promoted")
    assert [r.chunk_id for r in forward] == [r.chunk_id for r in reversed_]


# --- configuration is server-owned ------------------------------------------


def test_top_k_is_enforced() -> None:
    corpus = [chunk(f"{n:02d}::h::0::0", "shared term here") for n in range(20)]
    results = build_index(corpus, replace(CONFIG, top_k=3)).search("shared term")
    assert len(results) == 3


def test_minimum_score_causes_an_empty_result() -> None:
    """The signal 17.1c turns into an insufficient-evidence refusal."""
    index = build_index(CORPUS, replace(CONFIG, minimum_score=1000.0))
    assert index.search("Unity Catalog") == []


def test_search_takes_no_caller_override() -> None:
    """A caller must not be able to widen the evidence set."""
    import inspect

    parameters = inspect.signature(build_index(CORPUS, CONFIG).search).parameters
    assert list(parameters) == ["query"]


@pytest.mark.parametrize(("k1", "b"), [(0.5, 0.75), (2.0, 0.75), (1.2, 0.0), (1.2, 1.0)])
def test_configuration_values_change_scoring(k1: float, b: float) -> None:
    baseline = index_of(CORPUS).search("state stored account")[0].score
    tuned = (
        build_index(CORPUS, replace(CONFIG, bm25_k1=k1, bm25_b=b))
        .search("state stored account")[0]
        .score
    )
    assert baseline != pytest.approx(tuned) or (k1, b) == (1.2, 0.75)


def test_b_zero_disables_length_normalisation() -> None:
    short = chunk("s::h::0::0", "terraform state")
    long = chunk("l::h::0::0", "terraform state " + ("filler " * 200))
    with_norm = build_index([short, long], replace(CONFIG, bm25_b=1.0)).search("terraform state")
    without = build_index([short, long], replace(CONFIG, bm25_b=0.0)).search("terraform state")
    assert with_norm[0].chunk_id == "s::h::0::0"
    assert [r.chunk_id for r in without] == sorted(r.chunk_id for r in without)


# --- rejected inputs --------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\n\t "])
def test_empty_or_whitespace_query_is_rejected(query: str) -> None:
    with pytest.raises(RetrievalError):
        index_of(CORPUS).search(query)


def test_query_with_no_indexable_token_is_rejected() -> None:
    with pytest.raises(RetrievalError):
        index_of(CORPUS).search("--- /// ...")


def test_index_cannot_be_built_from_an_empty_corpus() -> None:
    with pytest.raises(RetrievalError):
        build_index([], CONFIG)


def test_index_cannot_be_built_when_every_chunk_tokenises_to_nothing() -> None:
    with pytest.raises(RetrievalError):
        build_index([chunk("a::h::0::0", "---"), chunk("b::h::0::0", "///")], CONFIG)


# --- authority is metadata, not a ranking signal ----------------------------


def test_authority_does_not_change_the_score() -> None:
    """Weighting relevance by authority would make retrieval unmeasurable alone."""
    as_adr = build_index([chunk("a::h::0::0", "unity catalog isolation", authority="adr")], CONFIG)
    as_runbook = build_index(
        [chunk("a::h::0::0", "unity catalog isolation", authority="runbook")], CONFIG
    )
    assert as_adr.search("unity catalog")[0].score == as_runbook.search("unity catalog")[0].score


def test_authority_survives_retrieval_as_metadata() -> None:
    """17.1c's conflict policy needs it intact."""
    results = build_index(
        [chunk("a::h::0::0", "unity catalog isolation", authority="runbook")], CONFIG
    ).search("unity catalog")
    assert results[0].chunk.authority == "runbook"


# --- results carry what 17.1c needs -----------------------------------------


def test_results_carry_chunk_metadata_and_text() -> None:
    result = index_of(CORPUS).search("Unity Catalog")[0]
    assert result.chunk.doc_id and result.chunk.doc_path and result.chunk.heading_path
    assert result.chunk.text
    assert result.score > 0


# --- the real corpus --------------------------------------------------------


def test_real_corpus_index_builds_and_answers() -> None:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    index = build_index(chunks, config)
    assert index.size == len(chunks)

    results = index.search("How many environments does the platform have?")
    assert results
    assert len(results) <= config.top_k


# --- field-aware scoring (17.1b hardening) ----------------------------------
#
# Every example below is generic: invented documents and invented queries. No
# test here names a corpus document, a fixture case id or an expected label.

META = [
    Chunk(
        chunk_id="a::intro::0::0",
        doc_id="alpha",
        doc_path="docs/alpha.md",
        title="Widget Calibration Standard",
        authority="standard",
        heading_path="Overview",
        section_occurrence=0,
        chunk_ordinal=0,
        content_sha256="0" * 64,
        text="Calibration is mentioned once in this short introduction.",
    ),
    Chunk(
        chunk_id="b::body::0::0",
        doc_id="beta",
        doc_path="docs/beta.md",
        title="Gadget Assembly Guide",
        authority="runbook",
        heading_path="Procedure > Calibration",
        section_occurrence=0,
        chunk_ordinal=0,
        content_sha256="1" * 64,
        text="Calibration calibration calibration, discussed at length and repeatedly.",
    ),
]


def test_zero_weights_reproduce_the_body_only_score_exactly() -> None:
    """The property that makes the baseline comparable across this change."""
    index = build_index(META, replace(CONFIG, title_weight=0.0, heading_weight=0.0))
    terms = tokenize("calibration gadgets")
    for position in range(len(META)):
        assert index.score_chunk(position, terms) == index.body_score(position, terms)


def test_zero_weights_reproduce_the_body_only_ranking() -> None:
    index = build_index(META, replace(CONFIG, title_weight=0.0, heading_weight=0.0))
    results = index.search("calibration gadgets")
    assert [r.score for r in results] == [
        index.body_score(META.index(r.chunk), tokenize("calibration gadgets")) for r in results
    ]


def test_title_weighting_can_promote_a_title_relevant_chunk() -> None:
    """Body frequency favours beta; the title says alpha is ABOUT calibration."""
    body_only = build_index(META, replace(CONFIG, title_weight=0.0, heading_weight=0.0))
    weighted = build_index(META, replace(CONFIG, title_weight=3.0, heading_weight=0.0))

    assert body_only.search("calibration")[0].chunk_id == "b::body::0::0"
    assert weighted.search("calibration")[0].chunk_id == "a::intro::0::0"


def test_heading_weighting_can_promote_a_heading_relevant_chunk() -> None:
    corpus = [
        Chunk(
            chunk_id="x::h::0::0",
            doc_id="x",
            doc_path="docs/x.md",
            title="Untitled",
            authority="standard",
            heading_path="Appendix",
            section_occurrence=0,
            chunk_ordinal=0,
            content_sha256="2" * 64,
            text="recovery recovery recovery mentioned many times here",
        ),
        Chunk(
            chunk_id="y::h::0::0",
            doc_id="y",
            doc_path="docs/y.md",
            title="Untitled",
            authority="standard",
            heading_path="Recovery Procedure",
            section_occurrence=0,
            chunk_ordinal=0,
            content_sha256="3" * 64,
            text="A single mention of recovery in passing.",
        ),
    ]
    body_only = build_index(corpus, replace(CONFIG, heading_weight=0.0))
    weighted = build_index(corpus, replace(CONFIG, heading_weight=5.0))

    assert body_only.search("recovery")[0].chunk_id == "x::h::0::0"
    assert weighted.search("recovery")[0].chunk_id == "y::h::0::0"


def test_metadata_that_does_not_match_the_query_adds_nothing() -> None:
    """A weight can only ever add to a chunk whose metadata actually matches."""
    weighted = build_index(META, replace(CONFIG, title_weight=5.0, heading_weight=5.0))
    plain = build_index(META, replace(CONFIG, title_weight=0.0, heading_weight=0.0))
    terms = tokenize("gadgets")  # in bodies, but not in alpha's title or heading
    assert weighted.score_chunk(0, terms) == plain.score_chunk(0, terms)


def test_repeating_a_query_term_does_not_inflate_the_metadata_component() -> None:
    """Distinct terms only, so a repeated word cannot buy a bigger boost."""
    index = build_index(META, replace(CONFIG, title_weight=2.0, heading_weight=2.0))
    once = index.score_chunk(0, tokenize("calibration"))
    twice = index.score_chunk(0, tokenize("calibration calibration"))
    body_once = index.body_score(0, tokenize("calibration"))
    body_twice = index.body_score(0, tokenize("calibration calibration"))
    assert (twice - body_twice) == pytest.approx(once - body_once)


def test_metadata_weighting_keeps_ties_deterministic() -> None:
    identical = [
        Chunk(
            chunk_id=f"{letter}::h::0::0",
            doc_id=letter,
            doc_path=f"docs/{letter}.md",
            title="Shared Title",
            authority="adr",
            heading_path="Shared Heading",
            section_occurrence=0,
            chunk_ordinal=0,
            content_sha256="4" * 64,
            text="identical body text",
        )
        for letter in "dbca"
    ]
    index = build_index(identical, replace(CONFIG, title_weight=1.0, heading_weight=1.0))
    results = index.search("shared identical")
    assert [r.chunk_id for r in results] == sorted(r.chunk_id for r in results)


def test_metadata_components_are_never_negative() -> None:
    index = build_index(META, replace(CONFIG, title_weight=2.0, heading_weight=2.0))
    terms = tokenize("calibration gadgets widget")
    for position in range(len(META)):
        assert index.score_chunk(position, terms) >= index.body_score(position, terms)


def test_document_frequency_is_computed_from_bodies_only() -> None:
    """Folding titles in would deflate the IDF the boost itself relies on."""
    index = build_index(META, CONFIG)
    # "widget" appears only in alpha's TITLE, never in any body.
    assert index._document_frequencies.get("widget", 0) == 0


def test_authority_still_does_not_affect_the_weighted_score() -> None:
    weighted = replace(CONFIG, title_weight=2.0, heading_weight=2.0)
    as_adr = (
        build_index([replace(META[0], authority="adr")], weighted).search("calibration")[0].score
    )
    as_runbook = (
        build_index([replace(META[0], authority="runbook")], weighted)
        .search("calibration")[0]
        .score
    )
    assert as_adr == as_runbook
