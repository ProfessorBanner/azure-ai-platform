"""The metadata-weight calibration sweep and its selection rule.

Asserts that the sweep is exhaustive, reproducible, redacted and that the
selection rule is applied mechanically. It deliberately does NOT pin a recall
figure: pinning today's number would turn a measurement into a target.
"""

from __future__ import annotations

from platform_engineering_assistant.config import load_retrieval_config
from platform_engineering_assistant.corpus import load_corpus, load_manifest
from platform_engineering_assistant.corpus.chunking import chunk_corpus
from platform_engineering_assistant.evaluation.calibration import (
    HEADING_WEIGHT_GRID,
    MINIMUM_RECALL_AT_5,
    TITLE_WEIGHT_GRID,
    GridPoint,
    baseline_of,
    evaluate_grid,
    render_grid,
    select_weights,
)
from platform_engineering_assistant.evaluation.retrieval_baseline import load_cases


def grid() -> list[GridPoint]:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    return evaluate_grid(chunks, load_cases(), config)


def test_grid_covers_every_published_combination() -> None:
    points = grid()
    assert len(points) == len(TITLE_WEIGHT_GRID) * len(HEADING_WEIGHT_GRID)
    combinations = {(point.title_weight, point.heading_weight) for point in points}
    assert combinations == {(t, h) for t in TITLE_WEIGHT_GRID for h in HEADING_WEIGHT_GRID}


def test_grid_includes_the_zero_weight_baseline() -> None:
    assert baseline_of(grid()).combined_weight == 0.0


def test_grid_is_reproducible() -> None:
    first, second = grid(), grid()
    assert [(p.title_weight, p.heading_weight, p.recall_at, p.missed) for p in first] == [
        (p.title_weight, p.heading_weight, p.recall_at, p.missed) for p in second
    ]


def test_selection_never_reduces_recall_at_1_or_3() -> None:
    points = grid()
    baseline = baseline_of(points)
    selected = select_weights(points)
    if selected is not None:
        assert selected.recall_at[1] >= baseline.recall_at[1]
        assert selected.recall_at[3] >= baseline.recall_at[3]
        assert selected.recall_at[5] >= MINIMUM_RECALL_AT_5


def test_selection_chooses_the_smallest_qualifying_combined_weight() -> None:
    points = grid()
    selected = select_weights(points)
    if selected is None:
        return
    baseline = baseline_of(points)
    qualifying = [
        point
        for point in points
        if point.combined_weight > 0
        and point.recall_at[5] >= MINIMUM_RECALL_AT_5
        and point.recall_at[1] >= baseline.recall_at[1]
        and point.recall_at[3] >= baseline.recall_at[3]
    ]
    assert selected.combined_weight == min(point.combined_weight for point in qualifying)


def test_selection_uses_non_zero_weights_only() -> None:
    selected = select_weights(grid())
    if selected is not None:
        assert selected.combined_weight > 0.0


def test_shipped_configuration_matches_the_selected_weights() -> None:
    """The configuration must be what the published rule actually chose."""
    selected = select_weights(grid())
    config = load_retrieval_config()
    if selected is None:
        assert config.title_weight == 0.0 and config.heading_weight == 0.0
    else:
        assert config.title_weight == selected.title_weight
        assert config.heading_weight == selected.heading_weight


def test_rendered_grid_states_it_is_not_proof_of_generalisation() -> None:
    points = grid()
    rendered = render_grid(points, select_weights(points))
    assert "not proof of generalisation" in rendered.lower()


def test_rendered_grid_reports_every_combination_including_worse_ones() -> None:
    points = grid()
    rendered = render_grid(points, select_weights(points))
    for point in points:
        assert f"{point.title_weight:6.2f} {point.heading_weight:6.2f}" in rendered


def test_rendered_grid_contains_no_question_or_chunk_text() -> None:
    config = load_retrieval_config()
    chunks = chunk_corpus(load_corpus(load_manifest()), config.chunk_budget_chars)
    points = evaluate_grid(chunks, load_cases(), config)
    rendered = render_grid(points, select_weights(points))

    for case in load_cases():
        assert case.question not in rendered
    for chunk in chunks[:40]:
        excerpt = chunk.text[:60].strip()
        if len(excerpt) > 30:
            assert excerpt not in rendered
