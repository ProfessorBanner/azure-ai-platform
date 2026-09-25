"""Manual live capability check: one real question against the real deployment.

This is the ONLY code path in the product that calls Azure, and it runs only
when a human invokes it. No test imports it for its `main()` behaviour and no
pipeline runs it: hosted agents cannot reach the IP-restricted account.

OUTPUT DISCIPLINE
-----------------
The answer text is NOT printed. The purpose is to prove the pipeline works end
to end — auth, retrieval, generation, grounding, citation construction — not to
display content. What it prints is the structural outcome plus telemetry, both
of which are safe to paste into a ticket.

Run:
    export AZURE_OPENAI_ENDPOINT="https://<subdomain>.openai.azure.com/openai/v1/"
    export AZURE_OPENAI_DEPLOYMENT="gpt-4-1-mini"
    uv run python -m platform_engineering_assistant.smoke "your question here"
"""

from __future__ import annotations

import sys

from platform_engineering_assistant.answering import build_service
from platform_engineering_assistant.domain import AnswerRequest, AnswerStatus
from platform_engineering_assistant.errors import AssistantError, FailureCategory

DEFAULT_QUESTION = "How is Terraform state separated between the platform environments?"

REMEDIATION: dict[FailureCategory, str] = {
    FailureCategory.CONFIGURATION: (
        "Check AZURE_OPENAI_ENDPOINT ends with /openai/v1/ and AZURE_OPENAI_DEPLOYMENT "
        "names a real deployment. Unset any API-key variable."
    ),
    FailureCategory.AUTHENTICATION: (
        "Run `az login`. If already signed in, confirm the tenant and that "
        "AZURE_OPENAI_AUTH_SCOPE matches the endpoint's expected audience."
    ),
    FailureCategory.AUTHORIZATION: (
        "Grant the signed-in principal the `Foundry User` role "
        "(53ca6127-db72-4b80-b1b0-d745d6d5456d) on the Foundry ACCOUNT. "
        "Owner/Contributor are control-plane only."
    ),
    FailureCategory.NETWORK_DENIED: (
        "Add this machine's public egress address to the capability root's "
        "allowed_ip_cidrs and re-apply."
    ),
    FailureCategory.RATE_LIMITED: "Deployment capacity exceeded. Retry, or raise capacity.",
    FailureCategory.TIMEOUT: "Raise AZURE_OPENAI_TIMEOUT_SECONDS, or check the network path.",
    FailureCategory.INVALID_STRUCTURED_OUTPUT: (
        "The model did not return the grounded-draft schema. Re-run; if persistent, "
        "the prompt or schema needs tightening."
    ),
    FailureCategory.CORPUS: "A corpus document failed admission. See the reported rule and path.",
    FailureCategory.PROVIDER_ERROR: "Transient or service-side failure. Re-run.",
}


def main(argv: list[str] | None = None) -> int:
    """Ask one question against the live deployment."""
    arguments = sys.argv[1:] if argv is None else argv
    question = " ".join(arguments).strip() or DEFAULT_QUESTION

    from platform_engineering_assistant.generation.azure_openai import (
        AzureOpenAIGenerationProvider,
    )
    from platform_engineering_assistant.provider_config import load_provider_config

    try:
        config = load_provider_config()
        service = build_service(AzureOpenAIGenerationProvider.from_config(config))
        result = service.answer(AnswerRequest(question=question))
    except AssistantError as error:
        guidance = REMEDIATION.get(error.category, "No specific guidance for this category.")
        print("Live capability check: FAILED", file=sys.stderr)
        print(f"  category={error.category}", file=sys.stderr)
        print(f"  detail={error}", file=sys.stderr)
        print(f"  next_step={guidance}", file=sys.stderr)
        return 1

    response = result.response
    print("Live capability check: SUCCESS")
    print(f"  {result.telemetry.summary()}")
    print(f"  status={response.status}")
    if response.status is AnswerStatus.ANSWERED:
        print(f"  answer_chars={len(response.answer or '')} (content not printed)")
        print(f"  citations={len(response.citations)}")
        for citation in response.citations:
            print(f"    - {citation.chunk_id}  [{citation.doc_path}]")
    else:
        print(f"  refusal_reason={response.refusal_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
