"""The offline provider: deterministic, protocol-conforming, and clearly labelled."""

from __future__ import annotations

from foundry_capability_lab.evaluation.dataset import load_cases
from foundry_capability_lab.evaluation.offline import (
    PROVIDER_LABEL,
    OfflineRiskAssessmentProvider,
)
from foundry_capability_lab.provider import RiskAssessmentProvider


def test_satisfies_the_provider_protocol() -> None:
    assert isinstance(OfflineRiskAssessmentProvider(), RiskAssessmentProvider)


def test_is_deterministic() -> None:
    provider = OfflineRiskAssessmentProvider()
    first = provider.assess("Alert ALERT-1: the job failed.")
    second = provider.assess("Alert ALERT-1: the job failed.")
    assert first.assessment == second.assessment
    assert first.telemetry.token_usage == second.telemetry.token_usage


def test_labels_itself_so_a_run_cannot_be_mistaken_for_a_real_one() -> None:
    result = OfflineRiskAssessmentProvider().assess("anything")
    assert result.telemetry.model_metadata.model == PROVIDER_LABEL


def test_extracts_evidence_verbatim_and_never_fabricates() -> None:
    """Offline runs must exercise the pipeline without tripping evidence scoring."""
    provider = OfflineRiskAssessmentProvider()
    for case in load_cases():
        assessment = provider.assess(case.observation).assessment
        assert set(assessment.evidence_ids) <= set(case.valid_evidence_ids), (
            f"{case.case_id}: offline provider cited unsupported evidence"
        )


def test_produces_schema_valid_output_for_every_shipped_case() -> None:
    provider = OfflineRiskAssessmentProvider()
    for case in load_cases():
        result = provider.assess(case.observation)
        assert result.telemetry.succeeded is True
        assert result.assessment.rationale


def test_reports_token_usage_so_telemetry_paths_are_exercised() -> None:
    usage = OfflineRiskAssessmentProvider().assess("Alert ALERT-1 fired.").telemetry.token_usage
    assert usage.input_tokens is not None
    assert usage.total_tokens == (usage.input_tokens or 0) + (usage.output_tokens or 0)
