"""Deterministic context construction and the untrusted-content boundary."""

from __future__ import annotations

import pytest

from platform_engineering_assistant.context import (
    CHUNK_SEPARATOR,
    EVIDENCE_BEGIN,
    EVIDENCE_END,
    build_context,
)
from tests.generation_fakes import RETRIEVED, result

BUDGET = 12000


def test_context_is_fenced_with_explicit_untrusted_delimiters() -> None:
    """The prompt can only say 'treat this as data' if 'this' is delimited."""
    built = build_context(RETRIEVED, BUDGET)
    assert built.text.startswith(EVIDENCE_BEGIN)
    assert built.text.endswith(EVIDENCE_END)
    assert "UNTRUSTED" in EVIDENCE_BEGIN


def test_every_chunk_carries_its_trusted_labels() -> None:
    built = build_context(RETRIEVED, BUDGET)
    for item in RETRIEVED:
        assert f"[chunk_id: {item.chunk.chunk_id}]" in built.text
        assert f"document: {item.chunk.doc_id}" in built.text
        assert f"authority: {item.chunk.authority}" in built.text


def test_chunk_boundaries_are_explicit() -> None:
    built = build_context(RETRIEVED, BUDGET)
    assert built.text.count(CHUNK_SEPARATOR) == len(RETRIEVED)


def test_order_follows_retrieval_order() -> None:
    built = build_context(RETRIEVED, BUDGET)
    positions = [built.text.index(f"[chunk_id: {r.chunk.chunk_id}]") for r in RETRIEVED]
    assert positions == sorted(positions)


def test_context_is_byte_identical_across_builds() -> None:
    assert build_context(RETRIEVED, BUDGET).text == build_context(RETRIEVED, BUDGET).text


def test_chunks_are_not_mutated() -> None:
    before = [(r.chunk.chunk_id, r.chunk.text) for r in RETRIEVED]
    build_context(RETRIEVED, BUDGET)
    assert [(r.chunk.chunk_id, r.chunk.text) for r in RETRIEVED] == before


def test_included_chunk_ids_are_exposed_for_grounding() -> None:
    """Only what the model was shown may be cited."""
    built = build_context(RETRIEVED, BUDGET)
    assert built.chunk_ids == tuple(r.chunk.chunk_id for r in RETRIEVED)


# --- budget -----------------------------------------------------------------


def test_budget_drops_whole_chunks_rather_than_cutting_one() -> None:
    """A truncated chunk reads as complete evidence while being partial."""
    long_chunks = [result(f"{n}::h::0::0", 9.0 - n, text="x" * 400) for n in range(6)]
    built = build_context(long_chunks, 900)

    assert built.dropped_for_budget > 0
    assert len(built.included) < len(long_chunks)
    for item in built.included:
        assert item.chunk.text in built.text  # present in full, never sliced


def test_budget_keeps_the_highest_ranked_chunks() -> None:
    long_chunks = [result(f"{n}::h::0::0", 9.0 - n, text="x" * 400) for n in range(6)]
    built = build_context(long_chunks, 900)
    assert built.included[0].chunk.chunk_id == "0::h::0::0"


def test_a_single_oversized_chunk_is_still_included_whole() -> None:
    """Better one oversized chunk than answering with no evidence."""
    built = build_context([result("big::h::0::0", 9.0, text="y" * 5000)], 500)
    assert len(built.included) == 1
    assert "y" * 5000 in built.text


def test_dropped_chunks_are_counted_not_silently_discarded() -> None:
    long_chunks = [result(f"{n}::h::0::0", 9.0 - n, text="x" * 400) for n in range(6)]
    built = build_context(long_chunks, 900)
    assert built.dropped_for_budget == len(long_chunks) - len(built.included)


def test_empty_retrieval_produces_an_empty_but_well_formed_block() -> None:
    built = build_context([], BUDGET)
    assert built.included == ()
    assert EVIDENCE_BEGIN in built.text and EVIDENCE_END in built.text


@pytest.mark.parametrize("budget", [0, -1])
def test_non_positive_budget_is_rejected(budget: int) -> None:
    with pytest.raises(ValueError):
        build_context(RETRIEVED, budget)


def test_context_repr_does_not_include_the_evidence_text() -> None:
    built = build_context(RETRIEVED, BUDGET)
    assert "Some approved documentation text" not in repr(built)
