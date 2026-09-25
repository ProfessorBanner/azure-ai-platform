"""Retrieval baseline: how good is retrieval BEFORE any model is involved.

This measures the half of a RAG system that a language model cannot rescue. If
the right document is not retrieved, no prompt and no model will produce a
grounded answer — it will produce a fluent one, which is worse. Measuring
retrieval on its own, offline and deterministically, is therefore the first
honest quality signal available, and it costs nothing to run.

There is no model call here and no Azure dependency of any kind.

REDACTION
---------
The report contains case ids, document ids, ranks, scores and length
statistics. It never contains chunk text. Question text is carried only inside
the version-controlled fixture, never written into a report.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from platform_engineering_assistant.config import PRODUCT_ROOT, RetrievalConfig
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.retrieval.index import BM25Index

DEFAULT_BASELINE_PATH = PRODUCT_ROOT / "evaluation" / "retrieval_baseline.jsonl"

RECALL_DEPTHS = (1, 3, 5)


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    """One labelled retrieval question."""

    case_id: str
    question: str
    expected_doc_ids: tuple[str, ...]
    topic: str
    must_refuse: bool = False


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """What retrieval did for one case. No text, by construction."""

    case_id: str
    must_refuse: bool
    returned: int
    top_score: float
    retrieved_doc_ids: tuple[str, ...]
    first_hit_rank: int | None
    # True when retrieval returned nothing at all. Named for what it is: an
    # empty result, not a decision to refuse.
    empty: bool


@dataclass(frozen=True, slots=True)
class BaselineReport:
    """Aggregate retrieval quality and corpus shape."""

    chunk_count: int
    duplicate_chunk_ids: int
    min_chunk_chars: int
    max_chunk_chars: int
    mean_chunk_chars: float
    median_chunk_chars: float
    over_budget_chunks: int

    answerable_cases: int
    out_of_scope_cases: int
    recall_at: dict[int, float]
    # NOT a refusal metric. Retrieval cannot decide whether evidence answers a
    # question; it only reports whether anything cleared the no-signal floor.
    # Genuine refusal is Phase 17.1c, where the question and the retrieved
    # evidence are judged together. Observational and ungated in 17.1b.
    out_of_scope_empty_retrieval_rate: float

    answerable_top_scores: tuple[float, ...]
    out_of_scope_top_scores: tuple[float, ...]

    outcomes: tuple[CaseOutcome, ...] = field(repr=False)

    @property
    def missed_cases(self) -> tuple[str, ...]:
        """Answerable cases where no expected document appeared in the top 5."""
        return tuple(
            outcome.case_id
            for outcome in self.outcomes
            if not outcome.must_refuse
            and (outcome.first_hit_rank is None or outcome.first_hit_rank > max(RECALL_DEPTHS))
        )


def load_cases(path: Path | None = None) -> list[RetrievalCase]:
    """Load the version-controlled retrieval fixture."""
    cases_path = DEFAULT_BASELINE_PATH if path is None else path
    try:
        raw_text = cases_path.read_text()
    except OSError as exc:
        raise ConfigurationError(
            f"Retrieval baseline fixture could not be read: {cases_path.name}"
        ) from exc

    cases: list[RetrievalCase] = []
    for number, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"{cases_path.name} line {number} is not valid JSON: {exc.msg}"
            ) from exc
        cases.append(
            RetrievalCase(
                case_id=str(payload["case_id"]),
                question=str(payload["question"]),
                expected_doc_ids=tuple(payload.get("expected_doc_ids", ())),
                topic=str(payload.get("topic", "")),
                must_refuse=bool(payload.get("must_refuse", False)),
            )
        )

    if not cases:
        raise ConfigurationError(f"{cases_path.name} contains no cases.")
    return cases


def _first_hit_rank(retrieved_doc_ids: tuple[str, ...], expected: tuple[str, ...]) -> int | None:
    """1-based rank of the first retrieved chunk from any expected document."""
    for rank, doc_id in enumerate(retrieved_doc_ids, start=1):
        if doc_id in expected:
            return rank
    return None


def measure_baseline(
    index: BM25Index,
    chunks: list[Chunk],
    cases: list[RetrievalCase],
    config: RetrievalConfig,
) -> BaselineReport:
    """Run every case through retrieval and aggregate. Pure; no I/O."""
    outcomes: list[CaseOutcome] = []

    for case in cases:
        results = index.search(case.question)
        retrieved_doc_ids = tuple(result.chunk.doc_id for result in results)
        top_score = results[0].score if results else 0.0

        outcomes.append(
            CaseOutcome(
                case_id=case.case_id,
                must_refuse=case.must_refuse,
                returned=len(results),
                top_score=top_score,
                retrieved_doc_ids=retrieved_doc_ids,
                first_hit_rank=_first_hit_rank(retrieved_doc_ids, case.expected_doc_ids),
                empty=not results,
            )
        )

    answerable = [outcome for outcome in outcomes if not outcome.must_refuse]
    out_of_scope = [outcome for outcome in outcomes if outcome.must_refuse]

    recall_at = {
        depth: (
            sum(
                1
                for outcome in answerable
                if outcome.first_hit_rank is not None and outcome.first_hit_rank <= depth
            )
            / len(answerable)
            if answerable
            else 0.0
        )
        for depth in RECALL_DEPTHS
    }

    empty_retrieval_rate = (
        sum(1 for outcome in out_of_scope if outcome.returned == 0) / len(out_of_scope)
        if out_of_scope
        else 0.0
    )

    lengths = [chunk.char_length for chunk in chunks]
    identifiers = [chunk.chunk_id for chunk in chunks]

    return BaselineReport(
        chunk_count=len(chunks),
        duplicate_chunk_ids=len(identifiers) - len(set(identifiers)),
        min_chunk_chars=min(lengths),
        max_chunk_chars=max(lengths),
        mean_chunk_chars=statistics.fmean(lengths),
        median_chunk_chars=statistics.median(lengths),
        over_budget_chunks=sum(1 for length in lengths if length > config.chunk_budget_chars),
        answerable_cases=len(answerable),
        out_of_scope_cases=len(out_of_scope),
        recall_at=recall_at,
        out_of_scope_empty_retrieval_rate=empty_retrieval_rate,
        answerable_top_scores=tuple(sorted(outcome.top_score for outcome in answerable)),
        out_of_scope_top_scores=tuple(sorted(outcome.top_score for outcome in out_of_scope)),
        outcomes=tuple(outcomes),
    )


def render_report(report: BaselineReport) -> str:
    """Render the baseline as plain text. Contains no chunk or question text."""
    scores = report.answerable_top_scores
    out_of_scope_scores = report.out_of_scope_top_scores

    lines = [
        "Retrieval baseline (offline, no model)",
        "",
        f"  chunks                 : {report.chunk_count}",
        f"  duplicate chunk ids    : {report.duplicate_chunk_ids}",
        f"  chunk chars min/med/mean/max : {report.min_chunk_chars} / "
        f"{report.median_chunk_chars:.0f} / {report.mean_chunk_chars:.0f} / "
        f"{report.max_chunk_chars}",
        f"  chunks over budget     : {report.over_budget_chunks}",
        "",
        f"  answerable cases       : {report.answerable_cases}",
        f"  out-of-scope cases     : {report.out_of_scope_cases}",
    ]
    for depth in RECALL_DEPTHS:
        lines.append(f"  recall@{depth}               : {report.recall_at[depth]:.1%}")
    lines += [
        f"  oos empty retrieval    : {report.out_of_scope_empty_retrieval_rate:.1%}"
        f"   (observational; not refusal — that is 17.1c)",
        "",
        "  answerable top scores  : "
        + (
            f"min {scores[0]:.2f} / median {statistics.median(scores):.2f} / max {scores[-1]:.2f}"
            if scores
            else "n/a"
        ),
        "  out-of-scope top scores: "
        + (
            f"min {out_of_scope_scores[0]:.2f} / max {out_of_scope_scores[-1]:.2f}"
            if out_of_scope_scores
            else "n/a"
        ),
    ]

    missed = report.missed_cases
    lines += [
        "",
        f"  missed (no expected doc in top {max(RECALL_DEPTHS)}): "
        + (", ".join(missed) if missed else "none"),
    ]
    return "\n".join(lines)


def main() -> int:
    """Build the corpus, index it and print the baseline. Offline only."""
    from platform_engineering_assistant.config import load_retrieval_config
    from platform_engineering_assistant.corpus import load_corpus, load_manifest
    from platform_engineering_assistant.corpus.chunking import chunk_corpus
    from platform_engineering_assistant.retrieval.index import build_index

    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    index = build_index(chunks, config)
    report = measure_baseline(index, chunks, load_cases(), config)
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
