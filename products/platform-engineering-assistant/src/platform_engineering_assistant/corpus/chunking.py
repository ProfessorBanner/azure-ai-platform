"""Deterministic, Markdown-aware chunking.

Determinism is the whole contract: the same document must always produce the
same chunks, with the same identifiers and the same hashes, on any machine and
in any order. Retrieval, citations and every later evaluation are built on that
assumption — a chunk id recorded in an evaluation case is worthless if the next
run renumbers it.

WHY HEADINGS
------------
Splitting on ATX headings keeps a chunk inside one topic, and gives every chunk
a heading path that is meaningful to a human reading a citation. "It says so in
ADR 0005" is not checkable; "ADR 0005 > Decision > Sandbox is an environment
class" is.

WHY NO OVERLAP
--------------
Overlapping windows duplicate text across chunks. In a lexical index that
inflates the document frequency of whatever was duplicated, so the very passages
that were split become systematically less findable. Overlap buys recall in
embedding search; here it would cost it.

WHY NO SPLITTING INSIDE FENCES
------------------------------
Half a Terraform block or half a bash command is not evidence — it is a
fragment that reads as complete and is not. Fenced blocks stay whole even when
that pushes a chunk past its budget.

REDACTION
---------
Nothing in this module logs, prints or embeds chunk text in an error message.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from platform_engineering_assistant.corpus.loader import LoadedDocument
from platform_engineering_assistant.errors import CorpusError, CorpusRule

# ATX headings only. Setext (underlined) headings are not used anywhere in this
# repository's documentation, and supporting a form the corpus does not contain
# would be untested code guarding an imaginary case.
_HEADING_PATTERN = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_FENCE_PATTERN = re.compile(r"^\s*(?P<fence>```|~~~)")
_SLUG_STRIP_PATTERN = re.compile(r"[^a-z0-9]+")

MAX_HEADING_DEPTH = 6


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable passage.

    `text` is carried internally for prompt composition and is deliberately
    never rendered into a report or a log line.
    """

    chunk_id: str
    doc_id: str
    doc_path: str
    title: str
    authority: str
    heading_path: str
    section_occurrence: int
    chunk_ordinal: int
    content_sha256: str
    text: str = field(repr=False)

    @property
    def char_length(self) -> int:
        return len(self.text)


def normalise_line_endings(text: str) -> str:
    """Collapse CRLF and lone CR to LF.

    Done before anything else so a document checked out with Windows line
    endings produces byte-identical chunks and hashes to the same document on
    Linux. Without this, `content_sha256` would depend on the checkout, not the
    content.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def slugify_heading_path(headings: list[str]) -> str:
    """Render a heading trail as a stable, filesystem-safe slug.

    Empty for the preamble before any heading, which is a real and common case:
    most documents open with a paragraph before their first `##`.
    """
    parts = [_SLUG_STRIP_PATTERN.sub("-", heading.lower()).strip("-") for heading in headings]
    return "--".join(part for part in parts if part)


def _readable_heading_path(headings: list[str]) -> str:
    return " > ".join(headings)


@dataclass
class _Section:
    """A heading and the lines beneath it, before any size-based splitting."""

    headings: list[str]
    lines: list[str]


def _split_into_sections(text: str) -> list[_Section]:
    """Walk the document once, tracking heading depth and fence state.

    Fence tracking matters here as well as during packing: a `#` inside a fenced
    block is a shell comment or a Terraform comment, not a heading, and treating
    it as one would silently restructure the document.
    """
    sections: list[_Section] = [_Section(headings=[], lines=[])]
    stack: list[str] = []
    open_fence: str | None = None

    for line in text.split("\n"):
        fence_match = _FENCE_PATTERN.match(line)
        if fence_match:
            fence = fence_match.group("fence")
            if open_fence is None:
                open_fence = fence
            elif fence == open_fence:
                open_fence = None
            sections[-1].lines.append(line)
            continue

        if open_fence is not None:
            sections[-1].lines.append(line)
            continue

        heading_match = _HEADING_PATTERN.match(line)
        if heading_match:
            depth = len(heading_match.group("hashes"))
            title = heading_match.group("title").strip()
            del stack[depth - 1 :]
            while len(stack) < depth - 1:
                # A document that jumps H1 -> H3 leaves a gap. Padding keeps the
                # path length equal to the depth so ancestry stays meaningful.
                stack.append("")
            stack.append(title)
            sections.append(_Section(headings=[h for h in stack if h], lines=[]))
            continue

        sections[-1].lines.append(line)

    return sections


def _paragraphs(lines: list[str]) -> list[str]:
    """Group lines into paragraphs, keeping fenced blocks as single units."""
    paragraphs: list[str] = []
    current: list[str] = []
    open_fence: str | None = None

    for line in lines:
        fence_match = _FENCE_PATTERN.match(line)
        if fence_match:
            fence = fence_match.group("fence")
            if open_fence is None:
                open_fence = fence
            elif fence == open_fence:
                open_fence = None
            current.append(line)
            continue

        if open_fence is None and not line.strip():
            if current:
                paragraphs.append("\n".join(current).strip())
                current = []
            continue

        current.append(line)

    if current:
        paragraphs.append("\n".join(current).strip())

    return [paragraph for paragraph in paragraphs if paragraph]


def _pack(paragraphs: list[str], budget: int) -> list[str]:
    """Greedily pack paragraphs into chunks without exceeding the budget.

    A single paragraph larger than the budget is emitted WHOLE rather than cut.
    Splitting mid-sentence, or mid-code-block, would produce a chunk that reads
    as complete evidence while being a fragment — the worst possible failure for
    something whose output gets cited.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for paragraph in paragraphs:
        addition = len(paragraph) + (2 if current else 0)
        if current and current_length + addition > budget:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_length = len(paragraph)
            continue
        current.append(paragraph)
        current_length += addition

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def chunk_document(document: LoadedDocument, budget_chars: int) -> list[Chunk]:
    """Split one approved document into ordered, identified chunks.

    Raises:
        CorpusError: if the document is empty or yields no usable chunk. A
            document that contributes nothing is a manifest error worth
            surfacing, not an empty list to be silently carried around.
    """
    if budget_chars <= 0:
        raise ValueError("budget_chars must be positive")

    normalised = normalise_line_endings(document.text)
    if not normalised.strip():
        raise CorpusError(
            CorpusRule.UNREADABLE,
            "Approved document is empty and can contribute no evidence.",
            path=document.path,
        )

    chunks: list[Chunk] = []
    # Counts how many times each heading path has already been seen in THIS
    # document. Several ADRs contain more than one "## Consequences", and
    # without this their chunk ids would collide.
    occurrences: dict[str, int] = {}

    for section in _split_into_sections(normalised):
        paragraphs = _paragraphs(section.lines)
        if not paragraphs:
            # A heading with no body: real (parent headings exist purely to
            # group) and deliberately produces no chunk rather than an empty one.
            continue

        slug = slugify_heading_path(section.headings)
        occurrence = occurrences.get(slug, 0)
        occurrences[slug] = occurrence + 1

        for ordinal, body in enumerate(_pack(paragraphs, budget_chars)):
            chunks.append(
                Chunk(
                    chunk_id=f"{document.doc_id}::{slug}::{occurrence}::{ordinal}",
                    doc_id=document.doc_id,
                    doc_path=document.path,
                    title=document.title,
                    authority=document.authority,
                    heading_path=_readable_heading_path(section.headings),
                    section_occurrence=occurrence,
                    chunk_ordinal=ordinal,
                    content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    text=body,
                )
            )

    if not chunks:
        raise CorpusError(
            CorpusRule.UNREADABLE,
            "Approved document produced no usable chunks.",
            path=document.path,
        )

    return chunks


def chunk_corpus(documents: list[LoadedDocument], budget_chars: int) -> list[Chunk]:
    """Chunk every document, preserving manifest order.

    Raises:
        CorpusError: on the first document that fails, or if any chunk id is
            duplicated across the corpus. A duplicate id would make a citation
            ambiguous, which defeats the point of citing.
    """
    if not documents:
        raise CorpusError(CorpusRule.MISSING, "Cannot chunk an empty corpus.")

    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document, budget_chars))

    identifiers = [chunk.chunk_id for chunk in chunks]
    if len(identifiers) != len(set(identifiers)):
        duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
        raise CorpusError(
            CorpusRule.DUPLICATE_PATH,
            f"Chunking produced duplicate chunk id(s): {', '.join(duplicates[:5])}",
        )

    return chunks
