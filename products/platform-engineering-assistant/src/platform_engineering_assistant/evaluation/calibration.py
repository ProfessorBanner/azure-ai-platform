"""Calibration of the metadata weights over an explicit, published grid.

WHAT THIS IS
------------
An exhaustive sweep of `title_weight` x `heading_weight` against the
version-controlled retrieval fixture, reporting every combination — including
the ones that are worse. Publishing the whole grid is the point: a single
"tuned" number with no visible alternatives is indistinguishable from a number
chosen to make a test pass.

WHAT THIS IS NOT
----------------
It is NOT evidence of generalisation. The fixture is a CALIBRATION set of
seventeen questions, written by the same person who wrote the retriever, and
every weight here is selected by looking at its scores. A weight that improves
recall on this fixture has been fitted to this fixture. Treating these numbers
as a quality guarantee would be exactly the mistake this docstring exists to
prevent; a held-out set written by someone else is the only thing that would
support that claim.

SELECTION RULE (applied mechanically, before looking at which case moved)
-------------------------------------------------------------------------
  1. recall@5 >= 0.85
  2. recall@1 must not fall below the zero-weight baseline
  3. recall@3 must not fall below the zero-weight baseline
  4. among survivors, the smallest combined non-zero weight
  5. if nothing qualifies, change nothing

No model, no network, no Azure.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from platform_engineering_assistant.config import RetrievalConfig
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.evaluation.retrieval_baseline import (
    BaselineReport,
    RetrievalCase,
    measure_baseline,
)
from platform_engineering_assistant.retrieval.index import build_index

TITLE_WEIGHT_GRID = (0.0, 0.5, 1.0, 2.0)
HEADING_WEIGHT_GRID = (0.0, 0.25, 0.5, 1.0)

MINIMUM_RECALL_AT_5 = 0.85


@dataclass(frozen=True, slots=True)
class GridPoint:
    """One weight combination and what it measured."""

    title_weight: float
    heading_weight: float
    recall_at: dict[int, float]
    missed: tuple[str, ...]
    out_of_scope_min_score: float
    out_of_scope_max_score: float

    @property
    def combined_weight(self) -> float:
        return self.title_weight + self.heading_weight


def evaluate_grid(
    chunks: list[Chunk], cases: list[RetrievalCase], config: RetrievalConfig
) -> list[GridPoint]:
    """Measure every combination. Deterministic and side-effect free."""
    points: list[GridPoint] = []
    for title_weight in TITLE_WEIGHT_GRID:
        for heading_weight in HEADING_WEIGHT_GRID:
            tuned = replace(config, title_weight=title_weight, heading_weight=heading_weight)
            report = measure_baseline(build_index(chunks, tuned), chunks, cases, tuned)
            out_of_scope = report.out_of_scope_top_scores
            points.append(
                GridPoint(
                    title_weight=title_weight,
                    heading_weight=heading_weight,
                    recall_at=dict(report.recall_at),
                    missed=report.missed_cases,
                    out_of_scope_min_score=out_of_scope[0] if out_of_scope else 0.0,
                    out_of_scope_max_score=out_of_scope[-1] if out_of_scope else 0.0,
                )
            )
    return points


def select_weights(points: list[GridPoint]) -> GridPoint | None:
    """Apply the published selection rule. Returns None when nothing qualifies.

    Returning None is a real outcome, not a failure: leaving the configuration
    alone is better than adopting weights that only look good on the set they
    were fitted to.
    """
    baseline = next(
        point for point in points if point.title_weight == 0.0 and point.heading_weight == 0.0
    )

    qualifying = [
        point
        for point in points
        if point.combined_weight > 0.0
        and point.recall_at[5] >= MINIMUM_RECALL_AT_5
        and point.recall_at[1] >= baseline.recall_at[1]
        and point.recall_at[3] >= baseline.recall_at[3]
    ]
    if not qualifying:
        return None

    # Smallest combined weight; ties broken deterministically by the individual
    # weights so the choice never depends on grid iteration order.
    qualifying.sort(key=lambda p: (p.combined_weight, p.title_weight, p.heading_weight))
    return qualifying[0]


def render_grid(points: list[GridPoint], selected: GridPoint | None) -> str:
    """Render the full grid. Contains no question or chunk text."""
    lines = [
        "Metadata-weight calibration grid",
        "",
        "  NOTE: a calibration set, not proof of generalisation. Weights are",
        "  fitted to these 17 questions by inspection of their own scores.",
        "",
        f"  {'title':>6} {'head':>6} {'r@1':>7} {'r@3':>7} {'r@5':>7}  "
        f"{'oos score range':>17}  missed",
        f"  {'-' * 6} {'-' * 6} {'-' * 7} {'-' * 7} {'-' * 7}  {'-' * 17}  {'-' * 24}",
    ]
    for point in points:
        marker = " *" if selected is not None and point is selected else "  "
        lines.append(
            f"{marker}{point.title_weight:6.2f} {point.heading_weight:6.2f} "
            f"{point.recall_at[1]:6.1%} {point.recall_at[3]:6.1%} {point.recall_at[5]:6.1%}  "
            f"{point.out_of_scope_min_score:7.2f}-{point.out_of_scope_max_score:<9.2f}  "
            f"{','.join(point.missed) if point.missed else 'none'}"
        )

    lines += ["", ""]
    if selected is None:
        lines.append(
            f"  No combination satisfied the rule (recall@5 >= {MINIMUM_RECALL_AT_5:.0%} "
            "without reducing recall@1 or recall@3). Configuration unchanged."
        )
    else:
        lines.append(
            f"  Selected: title_weight={selected.title_weight}, "
            f"heading_weight={selected.heading_weight} "
            f"(smallest combined weight satisfying the rule)."
        )
    return "\n".join(lines)


def baseline_of(points: list[GridPoint]) -> GridPoint:
    """The zero-weight point, i.e. pure body BM25."""
    return next(
        point for point in points if point.title_weight == 0.0 and point.heading_weight == 0.0
    )


def main() -> int:
    """Run the sweep and print the grid. Offline only."""
    from platform_engineering_assistant.config import load_retrieval_config
    from platform_engineering_assistant.corpus import load_corpus, load_manifest
    from platform_engineering_assistant.corpus.chunking import chunk_corpus
    from platform_engineering_assistant.evaluation.retrieval_baseline import load_cases

    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    cases = load_cases()

    points = evaluate_grid(chunks, cases, config)
    print(render_grid(points, select_weights(points)))
    return 0


def report_for(
    chunks: list[Chunk], cases: list[RetrievalCase], config: RetrievalConfig
) -> BaselineReport:
    """Convenience for tests: measure one configuration end to end."""
    return measure_baseline(build_index(chunks, config), chunks, cases, config)


if __name__ == "__main__":
    raise SystemExit(main())
