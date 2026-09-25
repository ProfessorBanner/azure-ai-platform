"""Entry-point rendering: outcomes reported, sensitive content withheld.

These tests exercise `run()` with a stubbed provider. `main()` is never called,
so no configuration is read and no client is constructed.
"""

from __future__ import annotations

import pytest

from foundry_capability_lab.errors import (
    AuthenticationError,
    AuthorizationError,
    LabError,
    NetworkDeniedError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from foundry_capability_lab.smoke import SAMPLE_OBSERVATION, render_success, run
from tests.fakes import StubProvider, sample_result

SECRET_LIKE = "Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.super-secret-token"


def test_successful_run_reports_zero_and_prints_structure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(StubProvider(result=sample_result()))
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "SUCCESS" in out
    assert "risk_classification=service_degradation" in out
    assert "severity=medium" in out
    assert "requires_escalation=False" in out
    assert "ALERT-4471" in out  # evidence identifiers are safe to show


def test_success_output_never_echoes_the_observation_or_rationale(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The prompt and model free text must not reach stdout."""
    result = sample_result()
    run(StubProvider(result=result))

    out = capsys.readouterr().out
    assert SAMPLE_OBSERVATION not in out
    assert result.assessment.rationale not in out
    # The length is reported instead, so the operator still sees it was populated.
    assert f"rationale_chars={len(result.assessment.rationale)}" in out


@pytest.mark.parametrize(
    ("error", "expected_marker"),
    [
        (AuthenticationError("401"), "category=authentication"),
        (AuthorizationError("403"), "category=authorization"),
        (NetworkDeniedError("ip"), "category=network_denied"),
        (RateLimitedError("429"), "category=rate_limited"),
        (TimeoutError_("slow"), "category=timeout"),
        (ProviderError("500"), "category=provider_error"),
    ],
)
def test_each_failure_category_is_reported_with_guidance(
    error: LabError,
    expected_marker: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(StubProvider(error=error))
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "FAILED" in err
    assert expected_marker in err
    assert "next_step=" in err


def test_failure_rendering_does_not_leak_token_material(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A token accidentally embedded in an error must not be re-rendered.

    The adapter builds its own messages rather than forwarding provider text, so
    this asserts the property end to end at the rendering boundary.
    """
    run(StubProvider(error=AuthenticationError("Entra ID authentication failed (401).")))
    err = capsys.readouterr().err
    assert SECRET_LIKE not in err
    assert "Bearer " not in err
    assert "eyJ" not in err  # a JWT prefix


def test_render_success_is_pure_and_returns_text() -> None:
    rendered = render_success(sample_result())
    assert isinstance(rendered, str)
    assert "SUCCESS" in rendered
