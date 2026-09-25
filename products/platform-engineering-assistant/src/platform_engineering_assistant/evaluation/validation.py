"""One-shot validation against the locked held-out set.

WHY A SEPARATE SET
------------------
The calibration grid selected `heading_weight` by reading its own scores, so
calibration recall is a fitted number and cannot say whether the choice was
sensible or an artefact of seventeen questions. `locked_validation_v1.json` was
written afterwards, from the source documents alone, and no retrieval was run
against any of its questions before it was finalised and hashed.

WHAT IT STILL IS NOT
--------------------
It is NOT statistically independent. The same engineering process wrote the
retriever, the corpus manifest, the calibration set AND these questions, so
shared vocabulary and shared assumptions are likely. It is a one-shot sanity
check, not evidence of production generalisation, and the fixture's own
provenance block says so.

RUN ONCE. Re-running after any change to weights, chunking or scoring destroys
its value as a held-out set, because the result would then have informed the
change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from platform_engineering_assistant.config import PRODUCT_ROOT, RetrievalConfig
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.evaluation.retrieval_baseline import (
    RECALL_DEPTHS,
    RetrievalCase,
)
from platform_engineering_assistant.retrieval.index import BM25Index

DEFAULT_VALIDATION_PATH = PRODUCT_ROOT / "evaluation" / "locked_validation_v1.json"

DATASET_ID = "locked_validation_v1"
EXPECTED_CASE_COUNT = 8
MINIMUM_RECALL_AT_5 = 0.85


@dataclass(frozen=True, slots=True)
class ValidationCase:
    """A held-out case plus the provenance that justifies its label."""

    case: RetrievalCase
    category: str
    evidence_path: str
    evidence_section: str
    evidence_lines: str
    evidence_why: str


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """What retrieval did for one case. Ids and ranks only; no text."""

    case_id: str
    category: str
    expected_doc_ids: tuple[str, ...]
    retrieved_doc_ids: tuple[str, ...]
    first_hit_rank: int | None
    top_score: float

    def hit_at(self, depth: int) -> bool:
        return self.first_hit_rank is not None and self.first_hit_rank <= depth


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Aggregate result of the single validation run."""

    fixture_sha256: str
    dataset_id: str
    recall_at: dict[int, float]
    outcomes: tuple[ValidationOutcome, ...]
    title_weight: float
    heading_weight: float

    @property
    def missed(self) -> tuple[str, ...]:
        return tuple(
            outcome.case_id for outcome in self.outcomes if not outcome.hit_at(max(RECALL_DEPTHS))
        )

    @property
    def passed_gate(self) -> bool:
        return self.recall_at[5] >= MINIMUM_RECALL_AT_5


def fixture_sha256(path: Path | None = None) -> str:
    """SHA-256 of the fixture bytes, so a report names the set it measured."""
    target = DEFAULT_VALIDATION_PATH if path is None else path
    return hashlib.sha256(target.read_bytes()).hexdigest()


def load_validation_set(path: Path | None = None) -> list[ValidationCase]:
    """Load and validate the locked fixture.

    Raises:
        ConfigurationError: if the file is missing, malformed, carries the wrong
            dataset id, or does not contain exactly the expected number of
            fully-provenanced cases.
    """
    target = DEFAULT_VALIDATION_PATH if path is None else path

    try:
        payload = json.loads(target.read_text())
    except OSError as exc:
        raise ConfigurationError(f"Validation fixture could not be read: {target.name}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{target.name} is not valid JSON: {exc.msg}") from exc

    if payload.get("dataset_id") != DATASET_ID:
        raise ConfigurationError(f"{target.name} is not the {DATASET_ID} dataset.")

    raw_cases = payload.get("cases", [])
    if len(raw_cases) != EXPECTED_CASE_COUNT:
        raise ConfigurationError(
            f"{target.name} must contain exactly {EXPECTED_CASE_COUNT} cases; "
            f"found {len(raw_cases)}."
        )

    cases: list[ValidationCase] = []
    for entry in raw_cases:
        evidence = entry.get("evidence", {})
        missing = [key for key in ("path", "section", "lines", "why") if not evidence.get(key)]
        if missing:
            raise ConfigurationError(
                f"{entry.get('case_id', '?')} is missing evidence field(s): {', '.join(missing)}"
            )
        if not entry.get("expected_doc_ids"):
            raise ConfigurationError(f"{entry.get('case_id', '?')} has no expected_doc_ids.")

        cases.append(
            ValidationCase(
                case=RetrievalCase(
                    case_id=str(entry["case_id"]),
                    question=str(entry["question"]),
                    expected_doc_ids=tuple(entry["expected_doc_ids"]),
                    topic=str(entry.get("category", "")),
                    must_refuse=False,
                ),
                category=str(entry["category"]),
                evidence_path=str(evidence["path"]),
                evidence_section=str(evidence["section"]),
                evidence_lines=str(evidence["lines"]),
                evidence_why=str(evidence["why"]),
            )
        )
    return cases


def run_validation(
    index: BM25Index,
    cases: list[ValidationCase],
    config: RetrievalConfig,
    path: Path | None = None,
) -> ValidationReport:
    """Measure the shipped configuration against the locked set. Read-only."""
    outcomes: list[ValidationOutcome] = []

    for entry in cases:
        results = index.search(entry.case.question)
        retrieved = tuple(result.chunk.doc_id for result in results)
        rank: int | None = None
        for position, doc_id in enumerate(retrieved, start=1):
            if doc_id in entry.case.expected_doc_ids:
                rank = position
                break
        outcomes.append(
            ValidationOutcome(
                case_id=entry.case.case_id,
                category=entry.category,
                expected_doc_ids=entry.case.expected_doc_ids,
                retrieved_doc_ids=retrieved,
                first_hit_rank=rank,
                top_score=results[0].score if results else 0.0,
            )
        )

    recall_at = {
        depth: sum(1 for outcome in outcomes if outcome.hit_at(depth)) / len(outcomes)
        for depth in RECALL_DEPTHS
    }

    return ValidationReport(
        fixture_sha256=fixture_sha256(path),
        dataset_id=DATASET_ID,
        recall_at=recall_at,
        outcomes=tuple(outcomes),
        title_weight=config.title_weight,
        heading_weight=config.heading_weight,
    )


def render_validation(report: ValidationReport) -> str:
    """Render the result. Contains no question text and no chunk text."""
    total = len(report.outcomes)
    hits_at_5 = sum(1 for outcome in report.outcomes if outcome.hit_at(5))

    lines = [
        "One-shot locked validation",
        "",
        f"  dataset          : {report.dataset_id}",
        f"  fixture sha256   : {report.fixture_sha256}",
        f"  weights          : title={report.title_weight}, heading={report.heading_weight}",
        "",
        f"  recall@1         : {report.recall_at[1]:.1%}",
        f"  recall@3         : {report.recall_at[3]:.1%}",
        f"  recall@5         : {report.recall_at[5]:.1%}  ({hits_at_5}/{total})",
        f"  gate (>= {MINIMUM_RECALL_AT_5:.0%})   : {'PASS' if report.passed_gate else 'FAIL'}",
        "",
        f"  {'case':9} {'category':26} {'expected':32} {'rank':>5} {'top':>7}",
        f"  {'-' * 9} {'-' * 26} {'-' * 32} {'-' * 5} {'-' * 7}",
    ]
    for outcome in report.outcomes:
        rank = str(outcome.first_hit_rank) if outcome.first_hit_rank else "miss"
        lines.append(
            f"  {outcome.case_id:9} {outcome.category:26} "
            f"{','.join(outcome.expected_doc_ids):32} {rank:>5} {outcome.top_score:7.2f}"
        )

    by_category: dict[str, list[ValidationOutcome]] = {}
    for outcome in report.outcomes:
        by_category.setdefault(outcome.category, []).append(outcome)

    lines += ["", "  by capability category:"]
    for category in sorted(by_category):
        group = by_category[category]
        hits = sum(1 for outcome in group if outcome.hit_at(5))
        lines.append(f"    {category:26} {hits}/{len(group)} at @5")

    lines += ["", f"  missed: {', '.join(report.missed) if report.missed else 'none'}"]
    lines += [
        "",
        "  Held out from calibration, but NOT statistically independent: the same",
        "  engineering process authored the retriever and these questions. A",
        "  one-shot sanity check, not a generalisation claim.",
    ]
    return "\n".join(lines)


def main() -> int:
    """Run the single validation pass against the shipped configuration."""
    from platform_engineering_assistant.config import load_retrieval_config
    from platform_engineering_assistant.corpus import load_corpus, load_manifest
    from platform_engineering_assistant.corpus.chunking import chunk_corpus
    from platform_engineering_assistant.retrieval.index import build_index

    config = load_retrieval_config()
    chunks: list[Chunk] = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    index = build_index(chunks, config)
    report = run_validation(index, load_validation_set(), config)
    print(render_validation(report))
    return 0 if report.passed_gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
