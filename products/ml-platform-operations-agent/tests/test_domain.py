"""Domain contracts: identifiers, time, NaN, arrays, aliases, invariants."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ml_platform_operations_agent.domain import (
    AliasBinding,
    CauseSupport,
    DegradationStatus,
    Diagnosis,
    EvidenceAbsence,
    EvidenceReference,
    LikelyCause,
    MetricThreshold,
    ModelVersionFacts,
    MonitoringObservation,
    PredictionSample,
    ResolvedModel,
    SourceType,
    TimeWindow,
    deduplicate,
    identifier_pattern,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=NOW - timedelta(days=7), end=NOW)


def reference(
    source_type: SourceType = SourceType.UNITY_CATALOG_TABLE,
    identifier: str = "dev.ml_lifecycle_demo.monitoring_history",
) -> EvidenceReference:
    return EvidenceReference(
        source_type=source_type,
        source_identifier=identifier,
        observed_at=NOW,
        query_window=WINDOW,
        relevant_fields=("rmse",),
    )


# --- time windows -----------------------------------------------------------


def test_naive_timestamps_are_rejected_not_assumed_utc() -> None:
    with pytest.raises(ValidationError):
        TimeWindow(start=datetime(2026, 9, 1), end=NOW)  # noqa: DTZ001


def test_offset_aware_input_is_normalised_to_utc() -> None:
    tokyo = timezone(timedelta(hours=9))
    window = TimeWindow(
        start=datetime(2026, 9, 1, 9, 0, tzinfo=tokyo),
        end=datetime(2026, 9, 3, 9, 0, tzinfo=tokyo),
    )
    assert window.start.tzinfo is UTC
    assert window.start == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def test_window_end_may_not_precede_start() -> None:
    with pytest.raises(ValidationError):
        TimeWindow(start=NOW, end=NOW - timedelta(days=1))


def test_boundary_timestamps_are_inside_the_window() -> None:
    """Inclusive at both ends: a row written exactly at the boundary is inside
    the window an operator asked about."""
    assert WINDOW.covers(WINDOW.start)
    assert WINDOW.covers(WINDOW.end)
    assert not WINDOW.covers(WINDOW.end + timedelta(microseconds=1))
    assert not WINDOW.covers(WINDOW.start - timedelta(microseconds=1))


def test_covers_rejects_a_naive_moment() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        WINDOW.covers(datetime(2026, 9, 2))  # noqa: DTZ001


def test_zero_length_window_is_valid_and_covers_its_instant() -> None:
    instant = TimeWindow(start=NOW, end=NOW)
    assert instant.duration_days == 0.0
    assert instant.covers(NOW)


# --- identifiers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "identifier"),
    [
        (SourceType.UNITY_CATALOG_TABLE, "dev.ml_lifecycle_demo.monitoring_history"),
        (SourceType.UNITY_CATALOG_MODEL, "dev.ml_lifecycle_demo.linear_regression_model"),
        (
            SourceType.UNITY_CATALOG_MODEL_VERSION,
            "dev.ml_lifecycle_demo.linear_regression_model/versions/7",
        ),
        (SourceType.MLFLOW_RUN, "b931f7114c2e4484831940fd2e7f4569"),
        (SourceType.JOB_RUN, "jobs/149357288315370/runs/170252815545026"),
        (SourceType.REPOSITORY_DOCUMENT, "docs/platform/ml-operations-runbook.md#the-thresholds"),
        (SourceType.REPOSITORY_SOURCE, "products/ml-lifecycle-demo/src/monitoring_logic.py"),
    ],
)
def test_valid_identifiers_are_accepted(source_type: SourceType, identifier: str) -> None:
    assert reference(source_type, identifier).source_identifier == identifier


@pytest.mark.parametrize(
    ("source_type", "identifier"),
    [
        # Free text is the thing this grammar exists to reject.
        (SourceType.UNITY_CATALOG_TABLE, "the monitoring history table"),
        (SourceType.UNITY_CATALOG_TABLE, "monitoring_history"),
        (SourceType.UNITY_CATALOG_TABLE, "dev.ml_lifecycle_demo"),
        (SourceType.MLFLOW_RUN, "not-a-run-id"),
        (SourceType.MLFLOW_RUN, "B931F7114C2E4484831940FD2E7F4569"),
        (SourceType.UNITY_CATALOG_MODEL_VERSION, "dev.s.m/versions/0"),
        # An identifier that escapes the repository names a document nobody
        # reviewed.
        (SourceType.REPOSITORY_DOCUMENT, "/etc/passwd.md"),
        (SourceType.REPOSITORY_DOCUMENT, "../../secrets.md"),
        (SourceType.REPOSITORY_DOCUMENT, "docs/runbook.txt"),
    ],
)
def test_unresolvable_identifiers_are_rejected(source_type: SourceType, identifier: str) -> None:
    with pytest.raises(ValidationError):
        reference(source_type, identifier)


def test_error_message_never_quotes_the_offending_identifier() -> None:
    """The message is logged and the identifier may be attacker-influenced."""
    secret = "dev.SECRETVALUE"
    with pytest.raises(ValidationError) as caught:
        reference(SourceType.UNITY_CATALOG_TABLE, secret)
    assert secret not in str(caught.value)


def test_a_citation_must_name_relevant_fields() -> None:
    with pytest.raises(ValidationError):
        EvidenceReference(
            source_type=SourceType.UNITY_CATALOG_TABLE,
            source_identifier="dev.s.t",
            observed_at=NOW,
            query_window=WINDOW,
            relevant_fields=(),
        )


def test_absence_identifiers_use_the_same_grammar() -> None:
    """An operator must be able to go and confirm the absence."""
    with pytest.raises(ValidationError):
        EvidenceAbsence(
            source_type=SourceType.UNITY_CATALOG_TABLE,
            source_identifier="the table that is missing",
            reason="absent",
            query_window=WINDOW,
        )


def test_identifier_pattern_is_exposed_for_independent_checking() -> None:
    assert identifier_pattern(SourceType.MLFLOW_RUN).match("0" * 32)


# --- deduplication ----------------------------------------------------------


def test_duplicate_evidence_collapses_preserving_order() -> None:
    first = reference(identifier="dev.s.a")
    second = reference(identifier="dev.s.b")
    assert deduplicate((first, second, first, second)) == (first, second)


def test_same_identifier_different_source_type_is_not_a_duplicate() -> None:
    table = reference(SourceType.UNITY_CATALOG_TABLE, "dev.s.linear_regression_model")
    model = reference(SourceType.UNITY_CATALOG_MODEL, "dev.s.linear_regression_model")
    assert len(deduplicate((table, model))) == 2


# --- thresholds and NaN -----------------------------------------------------


def test_nan_never_breaches_a_threshold() -> None:
    """A NaN metric is an absence of measurement, not a breach."""
    above = MetricThreshold(metric="rmse", breach_above=True, value=18.0, defined_in=reference())
    below = MetricThreshold(metric="r2", breach_above=False, value=0.85, defined_in=reference())
    assert not above.is_breached_by(math.nan)
    assert not below.is_breached_by(math.nan)


def test_infinity_breaches_an_upper_bound() -> None:
    above = MetricThreshold(metric="rmse", breach_above=True, value=18.0, defined_in=reference())
    assert above.is_breached_by(math.inf)
    assert not above.is_breached_by(-math.inf)


def test_a_threshold_value_must_be_finite() -> None:
    with pytest.raises(ValidationError):
        MetricThreshold(metric="rmse", breach_above=True, value=math.nan, defined_in=reference())


def test_exact_threshold_value_is_not_a_breach() -> None:
    """Strictly greater / strictly less, matching Phase 15's comparisons."""
    above = MetricThreshold(metric="rmse", breach_above=True, value=18.0, defined_in=reference())
    assert not above.is_breached_by(18.0)
    assert above.is_breached_by(18.000001)


# --- aliases ----------------------------------------------------------------


def test_alias_matching_is_case_insensitive_and_preserves_original() -> None:
    """Unity Catalog returns 'champion'; Phase 15 code writes 'Champion'."""
    binding = AliasBinding(name="champion", version=7)
    assert binding.matches("Champion")
    assert binding.matches("CHAMPION")
    assert binding.name == "champion"


def test_resolved_model_alias_lookup_is_case_insensitive() -> None:
    model = ResolvedModel(
        catalog="dev",
        schema_name="ml_lifecycle_demo",
        name="linear_regression_model",
        aliases=(AliasBinding(name="Champion", version=7),),
    )
    found = model.alias("champion")
    assert found is not None
    assert found.name == "Champion"
    assert model.alias("candidate") is None


def test_alias_binding_carries_no_time_range() -> None:
    """Unity Catalog exposes current alias state only. A temporal field here
    would be filled in by inference within a week."""
    assert "valid_from" not in AliasBinding.model_fields
    assert "valid_to" not in AliasBinding.model_fields
    assert not hasattr(ResolvedModel, "champion_version_at")


# --- array predictions ------------------------------------------------------


def test_single_element_array_yields_a_scalar() -> None:
    sample = PredictionSample(row_id=1.0, prediction=(12.5,), scored_at=NOW, model_version="7")
    assert sample.scalar == 12.5


def test_empty_array_has_no_scalar() -> None:
    sample = PredictionSample(row_id=1.0, prediction=(), scored_at=NOW, model_version="7")
    assert sample.scalar is None


def test_multi_element_array_refuses_rather_than_taking_the_first() -> None:
    """Silently reducing a multi-output prediction is a wrong number presented
    confidently."""
    sample = PredictionSample(row_id=1.0, prediction=(1.0, 2.0), scored_at=NOW, model_version="7")
    assert sample.scalar is None


# --- version facts ----------------------------------------------------------


def test_tags_available_is_distinct_from_tags_being_empty() -> None:
    """The CLI omits tags entirely while REST returns them. An adapter that
    could not tell the two apart would report provenance as missing."""
    lossy = ModelVersionFacts(version=7, created_at=NOW, tags={}, tags_available=False)
    genuinely_empty = ModelVersionFacts(version=7, created_at=NOW, tags={}, tags_available=True)
    assert lossy.tags == genuinely_empty.tags
    assert lossy.tags_available != genuinely_empty.tags_available


# --- diagnosis invariants ---------------------------------------------------


def test_degraded_without_evidence_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError):
        Diagnosis(
            requested_model="dev.s.m",
            resolved_model="dev.s.m",
            resolved_version=7,
            time_window=WINDOW,
            degradation_status=DegradationStatus.DEGRADED,
            summary="degraded",
            supporting_evidence=(),
        )


def test_insufficient_evidence_cannot_carry_high_confidence() -> None:
    with pytest.raises(ValidationError):
        Diagnosis(
            requested_model="dev.s.m",
            resolved_model=None,
            resolved_version=None,
            time_window=WINDOW,
            degradation_status=DegradationStatus.INSUFFICIENT_EVIDENCE,
            summary="unclear",
            confidence=0.9,
        )


def test_supported_cause_requires_evidence_and_mechanism() -> None:
    with pytest.raises(ValidationError):
        LikelyCause(statement="drift caused it", support=CauseSupport.SUPPORTED)

    with pytest.raises(ValidationError):
        LikelyCause(
            statement="drift caused it",
            support=CauseSupport.SUPPORTED,
            evidence=(reference(),),
        )

    LikelyCause(
        statement="drift caused it",
        support=CauseSupport.SUPPORTED,
        evidence=(reference(),),
        mechanism=reference(SourceType.REPOSITORY_DOCUMENT, "docs/platform/x.md"),
    )


def test_correlation_only_cause_needs_no_evidence() -> None:
    """It is not being asserted, so it carries no burden of proof."""
    cause = LikelyCause(statement="co-movement", support=CauseSupport.CORRELATION_ONLY)
    assert cause.evidence == ()


def test_monitoring_observation_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        MonitoringObservation(
            observed_at=datetime(2026, 9, 1),  # noqa: DTZ001
            model_version=7,
            status="ok",
        )


def test_monitoring_observation_allows_null_drift_score() -> None:
    """Phase 15 writes drift_score=None when drift is infinite."""
    row = MonitoringObservation(observed_at=NOW, model_version=7, status="ok", drift_score=None)
    assert row.drift_score is None
