"""The hosted server end to end, over the official protocol library.

These go through `ResponsesAgentServerHost` itself — its routing, its handler
contract, its readiness endpoint — with the Phase 18 agent behind it. The point
is not to test the library, but to prove the wiring is real: a hand-built
imitation would pass a hand-built test and fail in Foundry.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from hosted_agent.config import HostedAgentConfig
from hosted_agent.server import build_host
from tests.fakes import hosted_service, no_tool, propose_change, search

CONFIG = HostedAgentConfig(port=8088, agent_name="test-agent", max_input_chars=4000)
QUESTION = "How is Terraform state separated between environments?"


def client(*decisions: Any) -> TestClient:
    host = build_host(CONFIG, agent_factory=lambda: hosted_service(list(decisions)))
    return TestClient(host)


def ask(c: TestClient, question: str) -> dict[str, Any]:
    """POST a turn and read the completed response out of the SSE stream.

    The library streams by default, so the final `response.completed` event is
    where the metadata lands.
    """
    r = c.post("/responses", json={"input": question, "stream": True})
    assert r.status_code == 200, r.text
    completed: dict[str, Any] | None = None
    for line in r.text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        event = json.loads(payload)
        if event.get("type") in ("response.completed", "response.incomplete"):
            completed = event["response"]
    assert completed is not None, f"no completed event in stream:\n{r.text[:800]}"
    return completed


# --- the library owns the protocol surface ----------------------------------


def test_the_library_provides_the_responses_route() -> None:
    host = build_host(CONFIG, agent_factory=lambda: hosted_service(no_tool()))
    paths = {getattr(route, "path", "") for route in host.routes}
    assert "/responses" in paths


def test_the_library_provides_readiness() -> None:
    """Readiness is the library's, not hand-rolled."""
    host = build_host(CONFIG, agent_factory=lambda: hosted_service(no_tool()))
    paths = {getattr(route, "path", "") for route in host.routes}
    assert "/readiness" in paths
    with TestClient(host) as c:
        assert c.get("/readiness").status_code == 200


# --- a turn goes through the real Phase 18 agent ----------------------------


def test_a_question_is_answered_through_the_phase_18_agent() -> None:
    with client(no_tool()) as c:
        response = ask(c, QUESTION)
        assert response["metadata"]["outcome"] == "answered"
        assert int(response["metadata"]["citation_count"]) >= 1


def test_a_read_only_tool_turn_executes_and_is_reported() -> None:
    with client(search()) as c:
        metadata = ask(c, QUESTION)["metadata"]
        assert metadata["selected_tool"] == "search_platform_docs"
        assert metadata["tool_risk_level"] == "read_only"
        assert metadata["policy_decision"] == "allow"
        assert metadata["tool_execution_status"] == "succeeded"


def test_the_bounded_loop_is_unchanged_by_hosting() -> None:
    with client(search()) as c:
        assert int(ask(c, QUESTION)["metadata"]["tool_iterations"]) <= 2


# --- the controls survive the official server -------------------------------


def test_a_state_changing_proposal_still_stops_for_approval() -> None:
    """THE test. The protocol server changed; business authority did not."""
    with client(propose_change()) as c:
        response = ask(c, "Please raise a change request.")
        metadata = response["metadata"]
        assert metadata["outcome"] == "approval_required"
        assert metadata["policy_decision"] == "require_approval"
        assert metadata["tool_execution_status"] == "not_executed"
        assert metadata["approval_id"]

        text = json.dumps(response["output"])
        assert "NOT been carried out" in text


def test_the_approval_record_is_served_by_this_process() -> None:
    """Foundry hosts the process; the application holds the approval."""
    with client(propose_change()) as c:
        approval_id = ask(c, "Please raise a change request.")["metadata"]["approval_id"]

        listed = c.get("/v1/approvals").json()["pending"]
        assert [item["approval_id"] for item in listed] == [approval_id]

        record = c.get(f"/v1/approvals/{approval_id}").json()
        assert record["status"] == "pending"
        assert record["action"]["tool_name"] == "propose_change_request"
        assert len(record["action"]["argument_fingerprint"]) == 64


def test_an_unknown_approval_is_a_404() -> None:
    with client(no_tool()) as c:
        assert c.get("/v1/approvals/apr-nope").status_code == 404


# --- input bounds ------------------------------------------------------------


def test_an_over_long_input_is_refused_rather_than_truncated() -> None:
    """Truncating a question changes what was asked."""
    tight = HostedAgentConfig(port=8088, agent_name="t", max_input_chars=10)
    host = build_host(tight, agent_factory=lambda: hosted_service(no_tool()))
    with TestClient(host) as c:
        assert ask(c, "x" * 200)["metadata"]["outcome"] == "input_too_long"


def test_an_empty_question_is_refused_without_running_a_turn() -> None:
    with client() as c:
        assert ask(c, "   ")["metadata"]["outcome"] == "empty_request"


# --- startup ------------------------------------------------------------------


def test_a_broken_agent_fails_startup_rather_than_reporting_ready() -> None:
    """The library owns readiness, so a broken corpus must stop the process.

    A lazily-built agent would let the server report ready and then fail every
    turn, which is the worst of both.
    """
    from platform_engineering_assistant.errors import ConfigurationError

    def explode() -> Any:
        raise ConfigurationError("corpus missing")

    with pytest.raises(ConfigurationError):
        build_host(CONFIG, agent_factory=explode)


# --- unexpected defects stay observable, and stay inside the process ---------
#
# `AgentService.run` documents that it raises only `AssistantError`. Anything
# else is a bug — and before this guard existed such a bug escaped into the
# protocol server, which rendered an opaque `{"code": "server_error"}` with no
# correlation id. That is what made the 19.1e 500s undiagnosable.

DEFECT_DETAIL = "a defect quoting /app/products and a principal 00000000-dead-beef"


class _Exploding:
    """An agent whose `run` violates the contract. `approvals` is still real:
    the approval routes are mounted at build time and must not depend on the
    turn succeeding."""

    def __init__(self) -> None:
        self.approvals = hosted_service(no_tool()).approvals

    def run(self, request: Any, request_id: str) -> Any:
        raise RuntimeError(DEFECT_DETAIL)


def exploding_client() -> TestClient:
    return TestClient(build_host(CONFIG, agent_factory=_Exploding))


def test_an_unexpected_exception_becomes_a_structured_failure() -> None:
    """Not a 500, and not a traceback: a named outcome the caller can branch on."""
    with exploding_client() as c:
        completed = ask(c, QUESTION)
    metadata = completed["metadata"]
    assert metadata["outcome"] == "failed"
    assert metadata["failure_category"] == "internal_error"
    assert metadata["request_id"].startswith("req-")


def test_an_unexpected_exception_never_leaks_its_detail_to_the_caller() -> None:
    """A traceback carries file paths, and an exception message can quote the
    request, the corpus or a principal. The caller gets a correlation id."""
    with exploding_client() as c:
        r = c.post("/responses", json={"input": QUESTION, "stream": True})
    assert r.status_code == 200, r.text
    for leaked in (DEFECT_DETAIL, "Traceback", "RuntimeError", "/app/products"):
        assert leaked not in r.text, leaked


def test_the_correlation_id_is_offered_in_the_text_not_only_the_metadata() -> None:
    """A human reading the reply must be able to quote something to an operator."""
    with exploding_client() as c:
        completed = ask(c, QUESTION)
    request_id = completed["metadata"]["request_id"]
    assert f"request_id={request_id}" in json.dumps(completed)


def test_a_known_assistant_error_still_reports_its_own_category() -> None:
    """The new catch-all must not swallow the classified failures: an
    `AssistantError` keeps its category rather than degrading to
    `internal_error`."""
    from platform_engineering_assistant.errors import AuthenticationError

    class Unauthenticated(_Exploding):
        def run(self, request: Any, request_id: str) -> Any:
            raise AuthenticationError("401 from the model data plane.")

    with TestClient(build_host(CONFIG, agent_factory=Unauthenticated)) as c:
        metadata = ask(c, QUESTION)["metadata"]
    assert metadata["outcome"] == "failed"
    assert metadata["failure_category"] == "authentication"
    assert metadata["failure_category"] != "internal_error"
