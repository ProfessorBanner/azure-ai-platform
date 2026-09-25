"""Deterministic Markdown chunking."""

from __future__ import annotations

import pytest

from platform_engineering_assistant.config import load_retrieval_config
from platform_engineering_assistant.corpus import load_corpus, load_manifest
from platform_engineering_assistant.corpus.chunking import (
    Chunk,
    chunk_corpus,
    chunk_document,
    normalise_line_endings,
    slugify_heading_path,
)
from platform_engineering_assistant.corpus.loader import LoadedDocument
from platform_engineering_assistant.errors import CorpusError

BUDGET = 1200


def document(text: str, doc_id: str = "doc-1") -> LoadedDocument:
    return LoadedDocument(
        doc_id=doc_id, path="docs/doc.md", title="Doc", authority="adr", text=text
    )


def ids(chunks: list[Chunk]) -> list[str]:
    return [chunk.chunk_id for chunk in chunks]


# --- heading structure ------------------------------------------------------


def test_headings_become_separate_chunks() -> None:
    chunks = chunk_document(
        document("# Title\n\nIntro text.\n\n## Alpha\n\nAlpha body.\n\n## Beta\n\nBeta body.\n"),
        BUDGET,
    )
    assert [chunk.heading_path for chunk in chunks] == ["Title", "Title > Alpha", "Title > Beta"]


def test_full_heading_path_is_carried() -> None:
    text = "# A\n\nbody a\n\n## B\n\nbody b\n\n### C\n\nbody c\n"
    chunks = chunk_document(document(text), BUDGET)
    assert chunks[-1].heading_path == "A > B > C"


def test_deeper_heading_pops_back_to_the_right_ancestor() -> None:
    text = "# A\n\na\n\n## B\n\nb\n\n### C\n\nc\n\n## D\n\nd\n"
    chunks = chunk_document(document(text), BUDGET)
    assert chunks[-1].heading_path == "A > D"


def test_preamble_before_any_heading_is_chunked() -> None:
    """Most documents open with a paragraph before their first heading."""
    chunks = chunk_document(document("Loose intro paragraph.\n\n# Title\n\nBody.\n"), BUDGET)
    assert chunks[0].heading_path == ""
    assert chunks[0].chunk_id.startswith("doc-1::::0::0")


def test_heading_with_no_body_produces_no_chunk() -> None:
    """A grouping heading is real; an empty chunk is not."""
    chunks = chunk_document(document("# A\n\n## Empty\n\n## Full\n\nbody\n"), BUDGET)
    assert [chunk.heading_path for chunk in chunks] == ["A > Full"]


def test_document_with_only_headings_is_rejected() -> None:
    with pytest.raises(CorpusError):
        chunk_document(document("# A\n\n## B\n\n### C\n"), BUDGET)


def test_empty_document_is_rejected() -> None:
    with pytest.raises(CorpusError):
        chunk_document(document("   \n\n  \n"), BUDGET)


# --- repeated headings ------------------------------------------------------


def test_repeated_heading_paths_do_not_collide() -> None:
    """Several ADRs contain more than one '## Consequences'."""
    text = "# A\n\n## Consequences\n\nfirst\n\n## Other\n\nx\n\n## Consequences\n\nsecond\n"
    chunks = chunk_document(document(text), BUDGET)
    consequences = [c for c in chunks if c.heading_path.endswith("Consequences")]
    assert len(consequences) == 2
    assert consequences[0].section_occurrence == 0
    assert consequences[1].section_occurrence == 1
    assert consequences[0].chunk_id != consequences[1].chunk_id


def test_section_occurrence_appears_in_the_chunk_id() -> None:
    chunks = chunk_document(document("# A\n\n## S\n\nbody\n"), BUDGET)
    assert chunks[-1].chunk_id == "doc-1::a--s::0::0"


# --- determinism ------------------------------------------------------------


def test_identical_input_produces_identical_ids_and_hashes() -> None:
    text = "# A\n\nbody\n\n## B\n\nmore body\n"
    first = chunk_document(document(text), BUDGET)
    second = chunk_document(document(text), BUDGET)
    assert ids(first) == ids(second)
    assert [c.content_sha256 for c in first] == [c.content_sha256 for c in second]


def test_editing_a_later_section_leaves_earlier_ids_unchanged() -> None:
    """The reason ids are not byte offsets."""
    before = chunk_document(document("# A\n\nfirst\n\n## B\n\nsecond\n"), BUDGET)
    after = chunk_document(
        document("# A\n\nfirst\n\n## B\n\nsecond, now rewritten at much greater length.\n"),
        BUDGET,
    )
    assert ids(before)[0] == ids(after)[0]
    assert before[0].content_sha256 == after[0].content_sha256


def test_crlf_and_lf_produce_identical_chunks() -> None:
    """Otherwise a hash would depend on the checkout, not the content."""
    lf = "# A\n\nbody line one\nbody line two\n\n## B\n\nmore\n"
    crlf = lf.replace("\n", "\r\n")
    assert ids(chunk_document(document(lf), BUDGET)) == ids(chunk_document(document(crlf), BUDGET))
    assert [c.content_sha256 for c in chunk_document(document(lf), BUDGET)] == [
        c.content_sha256 for c in chunk_document(document(crlf), BUDGET)
    ]


def test_lone_cr_is_normalised() -> None:
    assert normalise_line_endings("a\rb\r\nc\n") == "a\nb\nc\n"


def test_source_order_is_preserved() -> None:
    text = "# A\n\none\n\n## B\n\ntwo\n\n## C\n\nthree\n"
    chunks = chunk_document(document(text), BUDGET)
    assert [c.text for c in chunks] == ["one", "two", "three"]


# --- splitting --------------------------------------------------------------


def test_oversized_section_splits_on_paragraph_boundaries() -> None:
    paragraph = "word " * 60  # ~300 chars
    text = "# A\n\n" + "\n\n".join(paragraph.strip() for _ in range(6)) + "\n"
    chunks = chunk_document(document(text), 700)
    assert len(chunks) > 1
    # No chunk begins or ends mid-paragraph.
    for chunk in chunks:
        assert not chunk.text.startswith("word word word word word word word word word word w ")
        assert chunk.text == chunk.text.strip()


def test_a_paragraph_longer_than_the_budget_is_never_cut() -> None:
    """A fragment that reads as complete evidence is the worst failure mode."""
    long_paragraph = "sentence. " * 300  # ~3000 chars
    chunks = chunk_document(document(f"# A\n\n{long_paragraph.strip()}\n"), 500)
    assert len(chunks) == 1
    assert chunks[0].char_length > 500


def test_no_overlapping_text_between_chunks() -> None:
    text = "# A\n\n" + "\n\n".join(f"paragraph number {n} " * 20 for n in range(8)) + "\n"
    chunks = chunk_document(document(text), 600)
    joined = "".join(chunk.text for chunk in chunks)
    # Total characters equals the sum of the parts: nothing is duplicated.
    assert len(joined) == sum(chunk.char_length for chunk in chunks)


def test_chunk_ordinals_are_sequential_within_a_section() -> None:
    text = "# A\n\n" + "\n\n".join("x" * 300 for _ in range(5)) + "\n"
    chunks = chunk_document(document(text), 700)
    assert [c.chunk_ordinal for c in chunks] == list(range(len(chunks)))


# --- fenced code blocks -----------------------------------------------------


def test_fenced_block_is_never_split() -> None:
    fence = "```hcl\n" + "\n".join(f'  attribute_{n} = "value"' for n in range(60)) + "\n```"
    chunks = chunk_document(document(f"# A\n\nintro\n\n{fence}\n"), 300)
    holder = [c for c in chunks if "```hcl" in c.text]
    assert len(holder) == 1
    assert holder[0].text.count("```") == 2


def test_hash_inside_a_fenced_block_is_not_a_heading() -> None:
    """A '#' in a shell block is a comment, not a section."""
    text = "# A\n\n```bash\n# this is a shell comment\nterraform plan\n```\n"
    chunks = chunk_document(document(text), BUDGET)
    assert len(chunks) == 1
    assert chunks[0].heading_path == "A"


def test_tilde_fences_are_supported() -> None:
    text = "# A\n\n~~~\n# not a heading\n~~~\n"
    chunks = chunk_document(document(text), BUDGET)
    assert len(chunks) == 1


# --- slugs ------------------------------------------------------------------


def test_slug_is_lowercase_and_punctuation_free() -> None:
    assert slugify_heading_path(["ADR 0005: Four-Environment!"]) == "adr-0005-four-environment"


def test_empty_heading_path_slugs_to_empty() -> None:
    assert slugify_heading_path([]) == ""


# --- the real corpus --------------------------------------------------------


def test_real_corpus_chunks_have_unique_ids() -> None:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    identifiers = [chunk.chunk_id for chunk in chunks]
    assert len(identifiers) == len(set(identifiers))
    assert len(chunks) > 100


def test_real_corpus_chunking_is_reproducible() -> None:
    config = load_retrieval_config()
    documents = load_corpus(load_manifest())
    first = chunk_corpus(documents, config.chunk_budget_chars)
    second = chunk_corpus(documents, config.chunk_budget_chars)
    assert ids(first) == ids(second)
    assert [c.content_sha256 for c in first] == [c.content_sha256 for c in second]


def test_every_real_chunk_carries_its_document_metadata() -> None:
    config = load_retrieval_config()
    for chunk in chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars):
        assert chunk.doc_id and chunk.doc_path and chunk.title and chunk.authority
        assert chunk.content_sha256
        assert chunk.text.strip()


def test_chunking_an_empty_corpus_is_rejected() -> None:
    with pytest.raises(CorpusError):
        chunk_corpus([], BUDGET)


def test_chunk_repr_does_not_include_its_text() -> None:
    """Chunk text must not leak through an accidental log of the object."""
    chunk = chunk_document(document("# A\n\nsensitive body text here\n"), BUDGET)[0]
    assert "sensitive body text here" not in repr(chunk)
