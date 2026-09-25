"""The Databricks App HTTP surface: `/responses`, `/health`, `/ready`.

WHY FASTAPI AND NOT AN "AGENT SERVER"
--------------------------------------
MLflow 3.16.0 provides the `ResponsesAgent` CONTRACT but no server for it, and
Databricks Apps needs a process listening on `DATABRICKS_APP_PORT`. So the
contract comes from MLflow (`ResponsesAgentRequest`/`Response`, used verbatim)
and only the transport is ours.

THIS MODULE IS DELIBERATELY THIN
---------------------------------
Parse, delegate, render, sanitise. It holds no policy: a request that should be
refused is refused by `policy.screen_request` inside the agent, not here. If
this file ever grows a rule about what may be asked, that rule is in the wrong
place and can be bypassed by anything that calls the agent directly.

NO STATE-CHANGING ROUTE EXISTS
-------------------------------
Only three routes, all GET or a single POST that reads. There is no endpoint
that writes, retrains, promotes or runs anything, and
`test_app_server.py::test_no_state_changing_route_exists` asserts the route
table rather than trusting this paragraph.

BINDING
-------
`0.0.0.0` on `DATABRICKS_APP_PORT`. Binding to localhost or a hardcoded port is
the documented cause of a 502 from the Apps proxy.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mlflow.types.responses import ResponsesAgentRequest

from ml_platform_operations_agent.agent import EvidenceSources
from ml_platform_operations_agent.app.responses_agent import (
    MlPlatformOperationsResponsesAgent,
)
from ml_platform_operations_agent.domain import TimeWindow
from ml_platform_operations_agent.errors import (
    InvalidRequestError,
    OperationsAgentError,
)

PORT_VAR = "DATABRICKS_APP_PORT"
DEFAULT_PORT = 8000

#: Hard cap on a request body. A Responses request is small; anything larger is
#: not a question this agent can answer.
MAX_BODY_BYTES = 64_000


def build_source_factory() -> Callable[[TimeWindow], EvidenceSources]:
    """Build a factory that produces live sources for a given request window.

    The SDK clients are constructed ONCE, here; only the window is rebound per
    request. Reconstructing clients per request would put an auth handshake on
    every call, and fixing the window at startup would stamp every absence
    record with a boot-time interval nobody asked about.

    Imported lazily and only when the server actually starts, so a test can
    build the app with fakes and never touch an SDK.
    """
    from pathlib import Path

    from databricks.sdk import WorkspaceClient
    from mlflow.tracking import MlflowClient

    from ml_platform_operations_agent.adapters.databricks import (
        DatabricksModelRegistry,
        DatabricksMonitoring,
        DatabricksRunHistory,
        RepositoryRunbooks,
    )
    from ml_platform_operations_agent.config import scope_for

    # In the App the SDK auto-configures from the INJECTED app identity —
    # `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` / `DATABRICKS_HOST`.
    # No profile is named and no token of ours is supplied, so there is no path
    # by which a developer's ~/.databrickscfg could be reached from a deployed
    # container. `WorkspaceClient()` with no arguments is the whole contract.
    workspace = WorkspaceClient()
    mlflow_client = MlflowClient(tracking_uri="databricks", registry_uri="databricks-uc")
    scope = scope_for(os.environ.get("ML_PLATFORM_CATALOG", "dev"))
    repository_root = Path(os.environ.get("ML_PLATFORM_REPO_ROOT", ".")).resolve()

    def factory(window: TimeWindow) -> EvidenceSources:
        registry = DatabricksModelRegistry(client=workspace, scope=scope, window=window)
        return EvidenceSources(
            registry_source=registry,
            run_history=DatabricksRunHistory(mlflow_client=mlflow_client, registry=registry),
            monitoring=DatabricksMonitoring(client=workspace, scope=scope),
            runbooks=RepositoryRunbooks(repository_root=repository_root, window=window),
        )

    return factory


def _configure_tracing() -> None:
    """Honour `ML_PLATFORM_AGENT_TRACING`, and DISABLE tracing when it is off.

    Not a no-op. MLflow instruments `ResponsesAgent.predict` automatically, so
    an App that merely declined to CONFIGURE tracing would still emit traces —
    to MLflow's default file store, creating an `mlruns/` directory inside the
    deployed source tree. Turning it off explicitly is what makes
    `ML_PLATFORM_AGENT_TRACING=disabled` mean what app.yaml says it means.
    """
    from ml_platform_operations_agent.tracing import TracingMode, resolve_config

    if resolve_config().mode is TracingMode.DISABLED:
        import mlflow.tracing

        mlflow.tracing.disable()


def create_app(
    sources: EvidenceSources | Callable[[TimeWindow], EvidenceSources] | None = None,
) -> FastAPI:
    """Build the ASGI app.

    `sources` is injected so every server test runs against the deterministic
    fakes and never needs a credential.
    """
    _configure_tracing()

    app = FastAPI(title="ml-platform-operations-agent", docs_url=None, redoc_url=None)
    agent = MlPlatformOperationsResponsesAgent(sources or build_source_factory())

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness. Answers without touching a workspace."""
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        """Readiness.

        Reports that the agent object exists and is read-only. It deliberately
        does NOT call Unity Catalog: a readiness probe that queried a workspace
        would fail the App during an unrelated control-plane blip and would put
        a metered call on a health check.
        """
        return {"status": "ready", "read_only": True, "runtime_model_calls": 0}

    @app.post("/responses")
    async def responses(request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _error(413, "invalid_request", "the request body is too large")

        try:
            payload = ResponsesAgentRequest.model_validate_json(raw)
        except Exception:
            # The parse error is NOT echoed: it quotes the offending body.
            return _error(400, "invalid_request", "the request is not a valid Responses payload")

        try:
            result = agent.predict(payload)
        except InvalidRequestError as error:
            return _error(400, "invalid_request", error.message)
        except OperationsAgentError as error:
            # Typed and sanitised: the category, never the underlying message.
            return _error(500, error.category.value, "the request could not be completed")
        except Exception:
            return _error(500, "internal_error", "the request could not be completed")

        return JSONResponse(result.model_dump(mode="json"))

    return app


def _error(status: int, code: str, message: str) -> JSONResponse:
    """A sanitised error. Names the rule, never the offending value."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def main() -> None:
    """Entry point named by `app.yaml`."""
    import uvicorn

    # 0.0.0.0 and the platform-supplied port. A hardcoded port or a localhost
    # bind is the documented cause of a 502 from the Apps proxy.
    port = int(os.environ.get(PORT_VAR, DEFAULT_PORT))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)  # noqa: S104


if __name__ == "__main__":
    main()
