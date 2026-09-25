"""In-process smoke check: the official server, over the Phase 18 agent.

Run by `validate.sh`. Deliberately not a pytest case — it is the thing a person
runs to see the package work, and it prints rather than asserts silently.
No network, no Azure, no credential.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
# Run as a script rather than under pytest, so the package root is not already
# on the path and `tests.fakes` would not otherwise resolve.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.testclient import TestClient  # noqa: E402

from hosted_agent.config import load_hosted_config  # noqa: E402
from hosted_agent.server import build_host  # noqa: E402
from tests.fakes import hosted_service, propose_change  # noqa: E402


def completed_response(raw_text: str) -> dict[str, object]:
    """Pull the final response object out of the SSE stream."""
    for line in raw_text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        event = json.loads(payload)
        if event.get("type") == "response.completed":
            response: dict[str, object] = event["response"]
            return response
    raise AssertionError(f"no completed event in stream:\n{raw_text[:600]}")


def main() -> int:
    host = build_host(
        load_hosted_config({}), agent_factory=lambda: hosted_service(propose_change())
    )
    paths = {getattr(route, "path", "") for route in host.routes}
    assert "/responses" in paths and "/readiness" in paths, paths

    with TestClient(host) as client:
        assert client.get("/readiness").status_code == 200
        print("   readiness: 200 (served by azure-ai-agentserver-responses)")

        raw = client.post(
            "/responses", json={"input": "Please raise a change request.", "stream": True}
        )
        metadata = completed_response(raw.text)["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["outcome"] == "approval_required", metadata
        assert metadata["tool_execution_status"] == "not_executed", metadata
        assert metadata["approval_id"]

        print(
            "   protocol outcome:",
            metadata["outcome"],
            "| executed:",
            metadata["tool_execution_status"],
        )
        print("   approval held by the application:", metadata["approval_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
