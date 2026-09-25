"""The orchestrator end to end, over the deterministic fixtures."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from ml_platform_operations_agent.adapters.fake import (
    NOW,
    SCENARIOS,
    FakeModelRegistry,
    FakeMonitoring,
    FakeRunbooks,
    FakeRunHistory,
    Scenario,
    window,
)
from ml_platform_operations_agent.agent import (
    DiagnosisRequest,
    EvidenceSources,
    OperationsAgent,
)
from ml_platform_operations_agent.domain import (
    AgentOutcome,
    CauseSupport,
    DegradationStatus,
    Diagnosis,
    RefusalReason,
)
from ml_platform_operations_agent.tools import DIAGNOSTIC_TOOL_ORDER


def diagnose(key: str, *, days: int = 7, question: str | None = None) -> Diagnosis:
    scenario: Scenario = SCENARIOS[key]
    agent = OperationsAgent(
        EvidenceSources(
            registry_source=scenario.registry,
            run_history=scenario.run_history,
            monitoring=scenario.monitoring,
            runbooks=scenario.runbooks,
        )
    )
    return agent.run(
        DiagnosisRequest(
            model_name=scenario.model_name,
            window=window(days),
            observed_at=NOW,
            question=question or scenario.question,
        )
    )


# --- the central claim ------------------------------------------------------


def test_supported_drift_asserts_a_cause_with_evidence_and_mechanism() -> None:
    result = diagnose("supported_drift")
    assert result.degradation_status is DegradationStatus.DEGRADED
    assert len(result.likely_causes) == 1
    cause = result.likely_causes[0]
    assert cause.support is CauseSupport.SUPPORTED
    assert cause.evidence
    assert cause.mechanism is not None


def test_identical_evidence_without_a_runbook_is_demoted_to_correlation() -> None:
    """The product's central claim: the SAME observations yield a supported
    cause only when a mechanism exists."""
    supported = diagnose("supported_drift")
    correlated = diagnose("correlation_only_drift")

    assert supported.degradation_status is correlated.degradation_status
    assert supported.likely_causes and not correlated.likely_causes
    assert correlated.recommended_investigations
    assert any("co-movement only" in item for item in correlated.recommended_investigations)


def test_version_change_is_never_asserted_as_a_root_cause() -> None:
    """A version change alongside a breach is co-movement; establishing cause
    would need a per-version comparison this evidence does not contain."""
    result = diagnose("version_change")
    assert all(c.support is CauseSupport.SUPPORTED for c in result.likely_causes)
    assert any("version" in item.casefold() for item in result.recommended_investigations)


# --- the three verdicts -----------------------------------------------------


def test_healthy_model_is_reported_not_degraded() -> None:
    result = diagnose("healthy")
    assert result.degradation_status is DegradationStatus.NOT_DEGRADED
    assert result.likely_causes == ()


def test_missing_monitoring_tables_abstain_and_say_why() -> None:
    """The live DEV situation. Absence is an answer, not an error."""
    result = diagnose("missing_monitoring_tables")
    assert result.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE
    assert result.outcome is AgentOutcome.DIAGNOSED
    assert any("monitoring table does not exist" in item for item in result.limitations)
    assert "monitoring_history" in " ".join(result.limitations)


def test_incomplete_window_abstains() -> None:
    result = diagnose("incomplete_window")
    assert result.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE


def test_stale_monitoring_abstains() -> None:
    result = diagnose("stale_monitoring")
    assert result.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE
    assert any("stopped" in item for item in result.limitations)


def test_unmeasurable_metrics_abstain_rather_than_report_health() -> None:
    result = diagnose("unmeasurable_metrics")
    assert result.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE


def test_abstention_never_carries_high_confidence() -> None:
    for key in ("missing_monitoring_tables", "incomplete_window", "stale_monitoring"):
        assert diagnose(key).confidence <= 0.5


def test_abstention_asserts_nothing_in_either_direction() -> None:
    result = diagnose("missing_monitoring_tables")
    assert "no degradation is asserted and none is ruled out" in result.summary.casefold()


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("retrain_request", RefusalReason.STATE_CHANGING_REQUEST),
        ("champion_change_request", RefusalReason.STATE_CHANGING_REQUEST),
        ("arbitrary_sql_request", RefusalReason.UNSAFE_INSTRUCTION),
    ],
)
def test_prohibited_requests_are_refused_before_any_tool_runs(
    key: str, reason: RefusalReason
) -> None:
    """A refusal that queried the workspace first came too late to have
    prevented anything."""
    result = diagnose(key)
    assert result.outcome is AgentOutcome.REFUSED
    assert result.refusal_reason is reason
    assert result.selected_tools == ()
    assert result.likely_causes == ()


def test_unknown_model_is_refused_not_diagnosed() -> None:
    result = diagnose("unknown_model")
    assert result.outcome is AgentOutcome.REFUSED
    assert result.refusal_reason is RefusalReason.UNKNOWN_MODEL
    assert result.resolved_model is None


def test_ungoverned_catalog_is_refused() -> None:
    scenario = SCENARIOS["healthy"]
    agent = OperationsAgent(
        EvidenceSources(
            scenario.registry, scenario.run_history, scenario.monitoring, scenario.runbooks
        )
    )
    result = agent.run(
        DiagnosisRequest(
            model_name="sandbox.ml_lifecycle_demo.linear_regression_model",
            window=window(7),
            observed_at=NOW,
            question="why did it degrade?",
        )
    )
    assert result.refusal_reason is RefusalReason.UNKNOWN_MODEL
    assert result.selected_tools == ()


def test_malformed_model_name_is_refused() -> None:
    scenario = SCENARIOS["healthy"]
    agent = OperationsAgent(
        EvidenceSources(
            scenario.registry, scenario.run_history, scenario.monitoring, scenario.runbooks
        )
    )
    for name in ("not-a-name", "dev.only_two", "dev.s.m.extra", ""):
        result = agent.run(
            DiagnosisRequest(
                model_name=name, window=window(7), observed_at=NOW, question="why degrade?"
            )
        )
        assert result.outcome is AgentOutcome.REFUSED


# --- injection --------------------------------------------------------------


@pytest.mark.parametrize("key", ["injection_in_monitoring", "injection_in_runbook"])
def test_injected_instructions_do_not_alter_the_tool_sequence(key: str) -> None:
    assert diagnose(key).selected_tools == DIAGNOSTIC_TOOL_ORDER


@pytest.mark.parametrize("key", ["injection_in_monitoring", "injection_in_runbook"])
def test_injected_text_is_never_echoed_into_the_output(key: str) -> None:
    rendered = json.dumps(diagnose(key).model_dump(mode="json")).casefold()
    for marker in ("ignore all previous", "you are now", "new instructions", "all privileges"):
        assert marker not in rendered


@pytest.mark.parametrize("key", ["injection_in_monitoring", "injection_in_runbook"])
def test_tampered_sources_are_annotated_as_a_limitation(key: str) -> None:
    assert any("instruction-like text" in item for item in diagnose(key).limitations)


def test_injection_does_not_change_the_verdict() -> None:
    """The injected scenario carries the same metrics as supported_drift; the
    verdict must come from the numbers, not the prose."""
    assert diagnose("injection_in_monitoring").degradation_status is DegradationStatus.DEGRADED


# --- aliases ----------------------------------------------------------------


def test_lowercase_alias_from_unity_catalog_resolves() -> None:
    assert diagnose("lowercase_alias").degradation_status is DegradationStatus.NOT_DEGRADED


def test_no_historical_alias_claim_is_made() -> None:
    result = diagnose("historical_alias_unknown")
    text = " ".join([result.summary, *(c.statement for c in result.likely_causes)]).casefold()
    for phrase in ("was champion", "had been champion", "previously champion"):
        assert phrase not in text


def test_registry_citation_states_the_alias_history_limitation() -> None:
    result = diagnose("missing_monitoring_tables")
    registry_refs = [
        r for r in result.supporting_evidence if r.source_type.value == "unity_catalog_model"
    ]
    assert registry_refs
    assert any("CURRENT state only" in lim for r in registry_refs for lim in r.limitations)


# --- failure handling -------------------------------------------------------


def test_a_failing_source_becomes_a_failed_outcome_not_a_crash() -> None:
    result = diagnose("source_failure")
    assert result.outcome is AgentOutcome.FAILED
    assert result.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE


def test_failure_output_leaks_no_exception_detail() -> None:
    """The adapter raised RuntimeError('simulated registry transport failure')."""
    rendered = json.dumps(diagnose("source_failure").model_dump(mode="json"))
    assert "simulated" not in rendered
    assert "RuntimeError" not in rendered
    assert "Traceback" not in rendered


# --- determinism and evidence hygiene ---------------------------------------


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_identical_input_produces_identical_output(key: str) -> None:
    assert diagnose(key).model_dump_json() == diagnose(key).model_dump_json()


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_only_read_only_tools_are_ever_selected(key: str) -> None:
    allowed = set(DIAGNOSTIC_TOOL_ORDER)
    assert set(diagnose(key).selected_tools) <= allowed


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_no_asserted_cause_is_unsupported(key: str) -> None:
    for cause in diagnose(key).likely_causes:
        assert cause.support is CauseSupport.SUPPORTED


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_supporting_evidence_is_deduplicated(key: str) -> None:
    evidence = diagnose(key).supporting_evidence
    keys = [r.key for r in evidence]
    assert len(keys) == len(set(keys))


def test_window_length_is_reflected_in_the_summary() -> None:
    assert "3.0-day" in diagnose("missing_monitoring_tables", days=3).summary


def test_a_longer_window_can_change_the_verdict() -> None:
    """Coverage is relative to the window asked about."""
    short = diagnose("incomplete_window", days=2)
    long = diagnose("incomplete_window", days=7)
    assert short.degradation_status is not long.degradation_status


# --- array predictions ------------------------------------------------------


def test_array_valued_predictions_do_not_break_the_diagnosis() -> None:
    result = diagnose("array_predictions")
    assert result.outcome is AgentOutcome.DIAGNOSED


# --- malformed evidence -----------------------------------------------------


def test_malformed_monitoring_rows_are_discarded_not_repaired() -> None:
    """A row we cannot parse is a row we cannot cite."""

    scenario = SCENARIOS["healthy"]
    broken = FakeMonitoring(
        observations=scenario.monitoring.observations[:1],  # one row -> no trend
    )
    agent = OperationsAgent(
        EvidenceSources(scenario.registry, scenario.run_history, broken, scenario.runbooks)
    )
    result = agent.run(
        DiagnosisRequest(
            model_name=scenario.model_name,
            window=window(7),
            observed_at=NOW,
            question="why did it degrade?",
        )
    )
    assert result.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE


def test_empty_sources_yield_abstention_not_failure() -> None:
    agent = OperationsAgent(
        EvidenceSources(
            FakeModelRegistry(SCENARIOS["healthy"].registry.model),
            FakeRunHistory(),
            FakeMonitoring(table_exists=False),
            FakeRunbooks(),
        )
    )
    result = agent.run(
        DiagnosisRequest(
            model_name=SCENARIOS["healthy"].model_name,
            window=window(7),
            observed_at=NOW,
            question="why did it degrade?",
        )
    )
    assert result.outcome is AgentOutcome.DIAGNOSED
    assert result.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE


def test_window_boundaries_are_respected_exactly() -> None:
    """A row exactly at the window start is inside it."""
    edge = window(7)
    assert edge.covers(edge.start)
    assert edge.covers(edge.end)
    assert not edge.covers(edge.start - timedelta(microseconds=1))
