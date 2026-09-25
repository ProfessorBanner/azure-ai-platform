"""BM25 retrieval over approved corpus chunks.

Implemented here rather than taken from a package, in about sixty lines of
stdlib arithmetic. The scoring function decides which evidence an answer may
rest on, so it should be readable, pinned and auditable in this repository
rather than a transitive dependency whose version could change what the product
retrieves.

DETERMINISM
-----------
Two queries with the same corpus, configuration and text must return the same
ordered results, always. Scores alone do not guarantee that — ties are common in
a small corpus — so the ordering key is `(-score, chunk_id)`, giving a total
order with no dependence on dict iteration, insertion order or float wobble.

CONFIGURATION IS SERVER-OWNED
-----------------------------
`k1`, `b`, `top_k` and `minimum_score` all come from the versioned retrieval
configuration. `search()` takes no override. Letting a caller widen `top_k` or
lower `minimum_score` would let them enlarge the evidence set until an answer
appeared, which is exactly the grounding property this product exists to
protect.

FIELD-AWARE SCORING
-------------------
Body BM25 alone answers "which passage uses these words most", which is not the
same question as "which document is ABOUT this". Measurement showed the gap
concretely: a query about the Terraform state backend retrieved the largest
document in the corpus, because size and term frequency beat topicality.

So the total score adds two metadata components:

    total = body_bm25
          + title_weight   * sum(idf(t) for distinct query terms t in the title)
          + heading_weight * sum(idf(t) for distinct query terms t in the heading)

Properties that make this safe to reason about:

  * The SAME corpus IDF is used throughout, so a term that is uninformative in
    the body is equally uninformative in a title: matching "the" earns nothing.
    A term appearing in a title but in no body is treated as maximally rare
    rather than as unseen, since it is more discriminating, not less.
  * Distinct query terms only. Repeating a word in the query cannot inflate a
    metadata match, and title tokens are never copied into the body stream,
    which would corrupt both term frequency and document frequency.
  * Both components are non-negative and exactly ZERO when nothing matches, so a
    weight of zero reproduces pure body BM25 bit for bit.
  * Nothing here is query- or document-specific. There is no synonym list, no
    expansion, and no reference to any evaluation fixture.

AUTHORITY IS NOT A RANKING SIGNAL
---------------------------------
Chunks carry an `authority` (adr, runbook, standard, current-state) and it is
carried through retrieval untouched. It deliberately does NOT affect the score.
Weighting relevance by authority would conflate two different questions — "is
this passage about what was asked?" and "how much should I trust it if sources
disagree?" — and would make retrieval quality impossible to measure on its own.
Conflict resolution is Phase 17.1c's problem, and it needs this metadata intact.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from platform_engineering_assistant.config import RetrievalConfig
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.errors import AssistantError, FailureCategory
from platform_engineering_assistant.retrieval.tokenize import tokenize


class RetrievalError(AssistantError):
    """The index could not be built, or a query was unusable."""

    category = FailureCategory.RETRIEVAL


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """One retrieved chunk and the score that admitted it.

    Carries the whole chunk, including its text, because 17.1c composes a prompt
    from exactly these. Reports must render the metadata only.
    """

    chunk: Chunk
    score: float

    @property
    def chunk_id(self) -> str:
        return self.chunk.chunk_id


@dataclass(frozen=True, slots=True)
class BM25Index:
    """An immutable BM25 index over approved chunks.

    Every field is computed once at construction. `search` reads and never
    writes, so concurrent queries cannot interfere and a query cannot leave the
    index different from how it found it.
    """

    chunks: tuple[Chunk, ...]
    config: RetrievalConfig
    _term_frequencies: tuple[Counter[str], ...] = field(repr=False)
    _document_frequencies: dict[str, int] = field(repr=False)
    _lengths: tuple[int, ...] = field(repr=False)
    _average_length: float = field(repr=False)
    # Distinct token sets for the metadata fields, precomputed once. Sets,
    # not counts: a title is a label, so how OFTEN a word appears is noise.
    _title_tokens: tuple[frozenset[str], ...] = field(repr=False)
    _heading_tokens: tuple[frozenset[str], ...] = field(repr=False)

    @property
    def size(self) -> int:
        return len(self.chunks)

    def _idf(self, term: str) -> float:
        """Robertson/Sparck-Jones IDF with the standard +0.5 smoothing.

        Clamped at zero: the unsmoothed form goes negative for a term appearing
        in more than half the corpus, and a negative contribution would let a
        very common term actively push a chunk DOWN the ranking, which is not
        what "this term is uninformative" should mean.
        """
        document_frequency = self._document_frequencies.get(term, 0)
        if document_frequency == 0:
            return 0.0
        total = len(self.chunks)
        raw = math.log(1.0 + (total - document_frequency + 0.5) / (document_frequency + 0.5))
        return max(raw, 0.0)

    def _field_idf(self, term: str) -> float:
        """IDF for a metadata match, where a term may be absent from every body.

        Body IDF returns 0.0 for an unseen term, which is right for body
        scoring (the term cannot contribute — its frequency is zero) but exactly
        backwards for metadata: a word that appears in a title and in NO body is
        rarer, and therefore more informative, than one appearing in a single
        body. Scoring it zero would silently disable the boost for precisely the
        most discriminating words.

        Such a term is therefore treated as if it had the lowest observed
        document frequency. Body scoring never reaches this path — it skips any
        term whose frequency is zero — so the body-only baseline is unaffected.
        """
        if self._document_frequencies.get(term, 0) > 0:
            return self._idf(term)
        total = len(self.chunks)
        return max(math.log(1.0 + (total - 1 + 0.5) / 1.5), 0.0)

    def body_score(self, index: int, query_terms: list[str]) -> float:
        """Pure BM25 over the chunk body. Unchanged from the 17.1b baseline."""
        frequencies = self._term_frequencies[index]
        length = self._lengths[index]
        k1 = self.config.bm25_k1
        b = self.config.bm25_b

        score = 0.0
        for term in query_terms:
            term_frequency = frequencies.get(term, 0)
            if term_frequency == 0:
                continue
            denominator = term_frequency + k1 * (1.0 - b + b * (length / self._average_length))
            score += self._idf(term) * (term_frequency * (k1 + 1.0)) / denominator
        return score

    def _field_score(self, tokens: frozenset[str], distinct_terms: frozenset[str]) -> float:
        """IDF mass of the distinct query terms present in a metadata field.

        Zero when nothing matches, which is what lets a weight of zero reproduce
        the body-only baseline exactly. Sorted for reproducible float addition.
        """
        return sum(self._field_idf(term) for term in sorted(distinct_terms & tokens))

    def score_chunk(self, index: int, query_terms: list[str]) -> float:
        """Total score: body BM25 plus weighted title and heading matches."""
        score = self.body_score(index, query_terms)

        title_weight = self.config.title_weight
        heading_weight = self.config.heading_weight
        if title_weight == 0.0 and heading_weight == 0.0:
            return score

        distinct = frozenset(query_terms)
        if title_weight:
            score += title_weight * self._field_score(self._title_tokens[index], distinct)
        if heading_weight:
            score += heading_weight * self._field_score(self._heading_tokens[index], distinct)
        return score

    def search(self, query: str) -> list[RetrievalResult]:
        """Return the top-scoring chunks for a query, or nothing.

        Returns an EMPTY list when the best score falls below `minimum_score` —
        the signal 17.1c turns into an insufficient-evidence refusal. Returning
        weak matches instead would invite an ungrounded answer built from
        whatever happened to rank first.

        Raises:
            RetrievalError: if the query is empty, whitespace, or contains no
                indexable token at all.
        """
        if not query or not query.strip():
            raise RetrievalError("Query must contain non-whitespace characters.")

        query_terms = tokenize(query)
        if not query_terms:
            raise RetrievalError("Query contains no indexable terms.")

        scored = [
            (self.score_chunk(index, query_terms), chunk) for index, chunk in enumerate(self.chunks)
        ]

        # Zero-score chunks share no term with the query; they are not weak
        # evidence, they are no evidence.
        candidates = [(score, chunk) for score, chunk in scored if score > 0.0]
        if not candidates:
            return []

        # Total order: score descending, then chunk_id ascending. The tie-break
        # is what makes repeated runs byte-identical.
        candidates.sort(key=lambda pair: (-pair[0], pair[1].chunk_id))

        if candidates[0][0] < self.config.minimum_score:
            return []

        return [
            RetrievalResult(chunk=chunk, score=score)
            for score, chunk in candidates[: self.config.top_k]
        ]


def build_index(chunks: list[Chunk], config: RetrievalConfig) -> BM25Index:
    """Build the index once, from approved chunks.

    Raises:
        RetrievalError: if the corpus is empty or every chunk tokenises to
            nothing. Either way there is nothing to retrieve, and failing here
            is better than answering from an empty index.
    """
    if not chunks:
        raise RetrievalError("Cannot build a retrieval index from an empty corpus.")

    frequencies: list[Counter[str]] = []
    lengths: list[int] = []
    document_frequencies: Counter[str] = Counter()
    title_tokens: list[frozenset[str]] = []
    heading_tokens: list[frozenset[str]] = []

    for chunk in chunks:
        tokens = tokenize(chunk.text)
        counter = Counter(tokens)
        frequencies.append(counter)
        lengths.append(len(tokens))
        # Document frequency is computed from the BODY only. Folding metadata in
        # would make title words look commoner than they are and quietly deflate
        # the IDF the metadata components themselves rely on.
        document_frequencies.update(counter.keys())
        title_tokens.append(frozenset(tokenize(chunk.title)))
        heading_tokens.append(frozenset(tokenize(chunk.heading_path)))

    total_length = sum(lengths)
    if total_length == 0:
        raise RetrievalError("Every chunk tokenised to nothing; the index would be empty.")

    return BM25Index(
        chunks=tuple(chunks),
        config=config,
        _term_frequencies=tuple(frequencies),
        _document_frequencies=dict(document_frequencies),
        _lengths=tuple(lengths),
        _average_length=total_length / len(chunks),
        _title_tokens=tuple(title_tokens),
        _heading_tokens=tuple(heading_tokens),
    )
