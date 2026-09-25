"""Telemetry representation and its log-safety guarantees."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry_capability_lab.errors import FailureCategory
from foundry_capability_lab.telemetry import (
    InvocationTelemetry,
    ModelMetadata,
    TokenUsage,
)
from tests.fakes import sample_result


def test_summary_reports_the_success_shape() -> None:
    summary = sample_result(latency_ms=412.5).telemetry.summary()
    assert summary.startswith("ok ")
    assert "deployment=gpt-4-1-mini" in summary
    assert "latency_ms=412.5" in summary
    assert "total_tokens=168" in summary
    assert "request_id=resp_fake_0001" in summary


def test_summary_reports_the_failure_category() -> None:
    telemetry = InvocationTelemetry(
        succeeded=False,
        latency_ms=88.0,
        model_metadata=ModelMetadata(deployment="gpt-4-1-mini"),
        failure_category=FailureCategory.NETWORK_DENIED,
    )
    summary = telemetry.summary()
    assert "failed:network_denied" in summary
    assert "latency_ms=88.0" in summary


def test_summary_omits_absent_optional_fields() -> None:
    telemetry = InvocationTelemetry(
        succeeded=True,
        latency_ms=10.0,
        model_metadata=ModelMetadata(deployment="d"),
    )
    summary = telemetry.summary()
    assert "total_tokens" not in summary
    assert "request_id" not in summary


def test_negative_latency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvocationTelemetry(
            succeeded=True,
            latency_ms=-1.0,
            model_metadata=ModelMetadata(deployment="d"),
        )


def test_telemetry_is_closed_to_unknown_fields() -> None:
    """Prevents a prompt or response body being attached to telemetry later."""
    with pytest.raises(ValidationError):
        InvocationTelemetry.model_validate(
            {
                "succeeded": True,
                "latency_ms": 1.0,
                "model_metadata": {"deployment": "d"},
                "prompt": "the observation text",
            }
        )


def test_serialised_telemetry_contains_only_safe_fields() -> None:
    """The whole record must be safe to write to a log or an artifact."""
    payload = sample_result().telemetry.model_dump()
    assert set(payload) == {
        "succeeded",
        "latency_ms",
        "model_metadata",
        "token_usage",
        "request_id",
        "failure_category",
    }
    assert set(payload["model_metadata"]) == {"deployment", "model", "api_contract"}
    assert set(payload["token_usage"]) == {"input_tokens", "output_tokens", "total_tokens"}


def test_token_usage_defaults_to_all_none() -> None:
    usage = TokenUsage()
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None
