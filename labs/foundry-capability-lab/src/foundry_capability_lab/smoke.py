"""Executable entry point for ONE live data-plane call.

This is the only module that performs I/O against Azure, and it runs only when a
human invokes it. Nothing in the automated test suite imports it for its
`main()` behaviour, and no pipeline runs it: hosted Azure DevOps agents cannot
reach the IP-restricted account anyway.

Output discipline: the observation and the model's free-text rationale are
NEVER printed. The point of the smoke test is to prove the call works and to
report how it behaved, not to display content. What it prints is the telemetry
summary plus the structural, non-free-text fields of the assessment.

Run:
    export AZURE_OPENAI_ENDPOINT="https://<subdomain>.openai.azure.com/openai/v1/"
    export AZURE_OPENAI_DEPLOYMENT="<deployment-name>"
    uv run python -m foundry_capability_lab.smoke
"""

from __future__ import annotations

import sys

from foundry_capability_lab.config import load_config
from foundry_capability_lab.errors import FailureCategory, LabError
from foundry_capability_lab.provider import (
    FoundryRiskAssessmentProvider,
    RiskAssessmentProvider,
)
from foundry_capability_lab.telemetry import InvocationResult

# A fixed, synthetic observation. Hard-coded rather than read from argv so that a
# routine capability check cannot become an accidental channel for real data.
SAMPLE_OBSERVATION = (
    "Alert ALERT-4471: the nightly ingestion job for the sandbox catalog failed "
    "three consecutive runs with a storage authorisation error. No downstream "
    "consumers have reported missing data yet. Related incident: INC-2208."
)

# Operator guidance per failure category. Keeping this next to the entry point
# means a failing smoke test tells its own operator what to do next.
REMEDIATION: dict[FailureCategory, str] = {
    FailureCategory.CONFIGURATION: (
        "Check AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT, and ensure "
        "no API-key variable is set."
    ),
    FailureCategory.AUTHENTICATION: (
        "Run `az login`. If already signed in, confirm the tenant and that "
        "AZURE_OPENAI_AUTH_SCOPE matches the endpoint's expected audience."
    ),
    FailureCategory.AUTHORIZATION: (
        "Grant the signed-in principal the `Foundry User` role "
        "(53ca6127-db72-4b80-b1b0-d745d6d5456d) scoped to the Foundry ACCOUNT "
        "resource. Owner/Contributor are control-plane only and do not grant "
        "Foundry data actions. This is a manual, approval-gated step."
    ),
    FailureCategory.NETWORK_DENIED: (
        "Add this machine's public egress address to the capability root's "
        "allowed_ip_cidrs and re-apply via the manual CD pipeline."
    ),
    FailureCategory.RATE_LIMITED: (
        "The deployment capacity is minimal by design. Retry, or raise deployment_capacity."
    ),
    FailureCategory.TIMEOUT: (
        "Increase AZURE_OPENAI_TIMEOUT_SECONDS, or check for a network path problem."
    ),
    FailureCategory.INVALID_STRUCTURED_OUTPUT: (
        "The model returned output that failed schema validation. Re-run; if it "
        "persists, the prompt or schema needs tightening."
    ),
    FailureCategory.PROVIDER_ERROR: (
        "Transient or service-side failure. Re-run; if it persists, quote the request_id above."
    ),
}


def render_success(result: InvocationResult) -> str:
    """Render a successful invocation without echoing free text."""
    assessment = result.assessment
    lines = [
        "Foundry data-plane smoke test: SUCCESS",
        f"  {result.telemetry.summary()}",
        f"  model={result.telemetry.model_metadata.model or 'not reported'}",
        f"  api_contract={result.telemetry.model_metadata.api_contract}",
        f"  input_tokens={result.telemetry.token_usage.input_tokens}",
        f"  output_tokens={result.telemetry.token_usage.output_tokens}",
        "  assessment:",
        f"    risk_classification={assessment.risk_classification}",
        f"    severity={assessment.severity}",
        f"    requires_escalation={assessment.requires_escalation}",
        f"    evidence_ids={assessment.evidence_ids}",
        f"    rationale_chars={len(assessment.rationale)} (content not printed)",
    ]
    return "\n".join(lines)


def render_failure(error: LabError) -> str:
    """Render a typed failure with its remedy, without echoing any payload."""
    guidance = REMEDIATION.get(error.category, "No specific guidance for this category.")
    return "\n".join(
        [
            "Foundry data-plane smoke test: FAILED",
            f"  category={error.category}",
            f"  detail={error}",
            f"  next_step={guidance}",
        ]
    )


def run(provider: RiskAssessmentProvider, observation: str = SAMPLE_OBSERVATION) -> int:
    """Invoke the provider and render the outcome. Returns a process exit code."""
    try:
        result = provider.assess(observation)
    except LabError as error:
        print(render_failure(error), file=sys.stderr)
        return 1
    print(render_success(result))
    return 0


def main() -> int:
    """Entry point for `python -m foundry_capability_lab.smoke`."""
    try:
        config = load_config()
        provider: RiskAssessmentProvider = FoundryRiskAssessmentProvider.from_config(config)
    except LabError as error:
        print(render_failure(error), file=sys.stderr)
        return 1
    return run(provider)


if __name__ == "__main__":
    raise SystemExit(main())
