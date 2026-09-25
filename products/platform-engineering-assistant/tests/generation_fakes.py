"""Shared builders for 17.1c tests. No network, no Azure, no credential."""

from __future__ import annotations

from platform_engineering_assistant.config import RetrievalConfig
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.retrieval.index import RetrievalResult

CONFIG = RetrievalConfig(
    version="retrieval_v1",
    top_k=6,
    minimum_score=0.0,
    bm25_k1=1.2,
    bm25_b=0.75,
    chunk_budget_chars=1200,
    title_weight=0.0,
    heading_weight=0.25,
)


def chunk(
    chunk_id: str,
    text: str = "Some approved documentation text.",
    *,
    doc_id: str = "adr-0001",
    doc_path: str = "docs/adr/0001-platform-scope.md",
    title: str = "ADR 0001",
    authority: str = "adr",
    heading_path: str = "Decision",
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_path=doc_path,
        title=title,
        authority=authority,
        heading_path=heading_path,
        section_occurrence=0,
        chunk_ordinal=0,
        content_sha256="0" * 64,
        text=text,
    )


def result(chunk_id: str, score: float = 5.0, **kwargs: object) -> RetrievalResult:
    return RetrievalResult(chunk=chunk(chunk_id, **kwargs), score=score)  # type: ignore[arg-type]


RETRIEVED = [
    result("a::decision::0::0", 9.0),
    result("b::context::0::0", 6.0, doc_id="adr-0002", doc_path="docs/adr/0002-x.md"),
    result("c::status::0::0", 3.0, doc_id="adr-0003", doc_path="docs/adr/0003-y.md"),
]
