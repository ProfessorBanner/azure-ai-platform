"""Deterministic construction of the evidence block shown to the model.

TRUST BOUNDARY
--------------
Everything inside the evidence block is UNTRUSTED. It is repository text that
happens to contain commands, configuration and prose that can read like
instructions. The block is therefore fenced with explicit delimiters and each
chunk is labelled, so the prompt can say "treat everything between these markers
as data" and mean something specific.

The labels themselves are TRUSTED: chunk id, document id, title, heading path
and authority all come from the corpus manifest and the chunker, never from the
model. That is what lets the server rebuild citations afterwards without
believing anything the model said about provenance.

BUDGET
------
Chunks are included whole, in retrieval order, until the next one would not fit.
A chunk is never cut. A truncated chunk reads as complete evidence while being
partial, which is the one failure mode worth spending budget to avoid — so when
the budget is exhausted the remaining chunks are dropped, and the caller is told
how many.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from platform_engineering_assistant.retrieval.index import RetrievalResult

EVIDENCE_BEGIN = "=== BEGIN UNTRUSTED EVIDENCE ==="
EVIDENCE_END = "=== END UNTRUSTED EVIDENCE ==="
CHUNK_SEPARATOR = "--- end of chunk ---"


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """The rendered evidence block and the exact chunks it contains."""

    text: str = field(repr=False)
    included: tuple[RetrievalResult, ...]
    dropped_for_budget: int

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        """The retrieved set a citation must belong to."""
        return tuple(result.chunk.chunk_id for result in self.included)


def _render_chunk(result: RetrievalResult) -> str:
    """Render one chunk with its trusted labels above its untrusted body."""
    chunk = result.chunk
    heading = chunk.heading_path or "(document preamble)"
    return "\n".join(
        [
            f"[chunk_id: {chunk.chunk_id}]",
            f"document: {chunk.doc_id} — {chunk.title}",
            f"authority: {chunk.authority}",
            f"heading: {heading}",
            "content:",
            chunk.text,
            CHUNK_SEPARATOR,
        ]
    )


def build_context(results: list[RetrievalResult], budget_chars: int) -> BuiltContext:
    """Render retrieved chunks into a bounded, delimited evidence block.

    Order is the retrieval order, which is already a total order (score then
    chunk id), so the same query always produces a byte-identical context.
    Chunks are never mutated.

    Raises:
        ValueError: if the budget is not positive.
    """
    if budget_chars <= 0:
        raise ValueError("budget_chars must be positive")

    included: list[RetrievalResult] = []
    rendered: list[str] = []
    used = 0
    dropped = 0

    for result in results:
        block = _render_chunk(result)
        cost = len(block) + 1
        if included and used + cost > budget_chars:
            # Every remaining chunk is lower-ranked, so stopping here loses the
            # least valuable evidence. Counted rather than silently discarded.
            dropped += 1
            continue
        if not included and cost > budget_chars:
            # The single best chunk does not fit. Including it whole is still
            # right: a truncated chunk is worse than an oversized one, and the
            # alternative is answering with no evidence at all.
            included.append(result)
            rendered.append(block)
            used += cost
            continue
        included.append(result)
        rendered.append(block)
        used += cost

    body = "\n".join(rendered)
    text = (
        f"{EVIDENCE_BEGIN}\n{body}\n{EVIDENCE_END}"
        if rendered
        else (f"{EVIDENCE_BEGIN}\n{EVIDENCE_END}")
    )

    return BuiltContext(text=text, included=tuple(included), dropped_for_budget=dropped)
