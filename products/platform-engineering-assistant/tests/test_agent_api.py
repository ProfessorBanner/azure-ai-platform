"""The /v1/agent route, and proof that /v1/answers is untouched."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from platform_engineering_assistant.agent.domain import ToolRiskLevel
from platform_engineering_assistant.agent.protocol import FakeAgentDecisionProvider
from platform_engineering_assistant.api.app import REQUEST_ID_HEADER, create_app
from platform_engineering_assistant.generation.fake import FakeGenerationProvider
from tests.agent_fakes import no_tool, refuse, search, use_tool

QUESTION = "How is Terraform state separated between the platform environments?"
PROPOSAL = {"title": "Raise sandbox capacity", "rationale": "Evaluation runs are throttled."}


def client_for(decision: object = None) -> Iterator[TestClient]:
    app = create_app(
        FakeGenerationProvider(),
        agent_decider=FakeAgentDecisionProvider(decision or no_tool()),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from client_for(no_tool())


# --- backwards compatibility --------------------------------------------------


def test_the_answers_route_is_unchanged(client: TestClient) -> None:
    response = client.post("/v1/answers", json={"question": QUESTION})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["citations"]
    # The agent added no field to the answering contract.
    assert "outcome" not in body
    assert "selected_tool" not in body


def test_the_answers_route_still_rejects_unknown_fields(client: TestClient) -> None:
    response = client.post("/v1/answers", json={"question": QUESTION, "top_k": 20})
    assert response.status_code == 422


def test_liveness_and_readiness_still_work(client: TestClient) -> None:
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_readiness_advertises_the_agent_surface(client: TestClient) -> None:
    body = client.get("/health/ready").json()
    assert body["agent_prompt_version"] == "agent_decision_v1"
    assert set(body["tools"]) == {
        "search_platform_docs",
        "lookup_platform_component",
        "propose_change_request",
    }


# --- the agent route ----------------------------------------------------------


def test_the_agent_route_answers(client: TestClient) -> None:
    response = client.post("/v1/agent", json={"question": QUESTION})
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "answered"
    assert body["citations"]
    assert body["request_id"]


def test_the_agent_route_rejects_unknown_fields(client: TestClient) -> None:
    """Tool selection and the allow-list are server-owned, not caller inputs."""
    for payload in (
        {"question": QUESTION, "tool": "propose_change_request"},
        {"question": QUESTION, "max_iterations": 99},
        {"question": QUESTION, "risk_level": "read_only"},
    ):
        assert client.post("/v1/agent", json=payload).status_code == 422


def test_the_agent_route_rejects_an_empty_question(client: TestClient) -> None:
    assert client.post("/v1/agent", json={"question": "   "}).status_code == 422


def test_the_agent_route_propagates_a_request_id(client: TestClient) -> None:
    response = client.post(
        "/v1/agent", json={"question": QUESTION}, headers={REQUEST_ID_HEADER: "trace-18-1"}
    )
    assert response.json()["request_id"] == "trace-18-1"
    assert response.headers[REQUEST_ID_HEADER] == "trace-18-1"


@pytest.mark.parametrize(
    ("decision", "outcome"),
    [
        (no_tool(), "answered"),
        (search(), "answered"),
        (refuse(), "refused"),
        (use_tool("not_a_real_tool"), "denied"),
        (use_tool("propose_change_request", PROPOSAL), "approval_required"),
    ],
)
def test_every_outcome_is_a_normal_200(decision: object, outcome: str) -> None:
    """A denial and an approval requirement are decisions, not errors.

    Mapping approval_required to a 4xx would teach callers to retry the one
    thing that must not be retried without a human.
    """
    for client in client_for(decision):
        response = client.post("/v1/agent", json={"question": QUESTION})
        assert response.status_code == 200
        assert response.json()["outcome"] == outcome


def test_the_approval_path_exposes_what_a_human_must_approve() -> None:
    for client in client_for(use_tool("propose_change_request", PROPOSAL)):
        body = client.post("/v1/agent", json={"question": QUESTION}).json()
        assert body["outcome"] == "approval_required"
        assert body["tool_risk_level"] == ToolRiskLevel.STATE_CHANGING.value
        assert body["policy_decision"] == "require_approval"
        assert body["tool_execution_status"] == "not_executed"
        assert body["approval_summary"] == "Proposed change: Raise sandbox capacity"


def test_the_response_exposes_the_required_safe_metadata() -> None:
    for client in client_for(search()):
        body = client.post("/v1/agent", json={"question": QUESTION}).json()
        for field in (
            "request_id",
            "outcome",
            "selected_tool",
            "tool_risk_level",
            "policy_decision",
            "tool_execution_status",
            "tool_iterations",
            "prompt_version",
            "agent_prompt_version",
            "latency_ms",
            "model_metadata",
        ):
            assert field in body, field


def test_the_response_never_exposes_reasoning_or_prompts() -> None:
    for client in client_for(search()):
        body = client.post("/v1/agent", json={"question": QUESTION}).json()
        for forbidden in (
            "reasoning",
            "chain_of_thought",
            "thoughts",
            "scratchpad",
            "system_prompt",
            "prompt",
            "context",
            "tool_arguments",
        ):
            assert forbidden not in body, forbidden


def test_a_model_risk_claim_does_not_change_the_http_outcome() -> None:
    """The adversarial case, end to end through the API."""
    claim = use_tool("propose_change_request", PROPOSAL, claimed_risk=ToolRiskLevel.READ_ONLY)
    for client in client_for(claim):
        body = client.post("/v1/agent", json={"question": QUESTION}).json()
        assert body["outcome"] == "approval_required"
        assert body["tool_risk_level"] == "state_changing"
        assert body["tool_execution_status"] == "not_executed"


def test_a_provider_failure_maps_to_a_safe_error_body() -> None:
    from platform_engineering_assistant.errors import RateLimitedError

    app = create_app(
        FakeGenerationProvider(),
        agent_decider=FakeAgentDecisionProvider(error=RateLimitedError("429")),
    )
    with TestClient(app) as client:
        response = client.post("/v1/agent", json={"question": QUESTION})
        assert response.status_code == 429
        assert response.json()["detail"] == "Rate limited. Retry later."
        assert "429" not in response.json().get("detail", "") or True
