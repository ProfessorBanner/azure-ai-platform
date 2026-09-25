"""The HTTP surface: three routes, strict validation, deliberate error mapping.

WHAT A CALLER CAN SEND
----------------------
A question. That is the entire input surface. `AnswerRequest` is a closed model,
so `top_k`, `model`, `prompt`, `deployment`, `citations`, `authority`, document
ids and context are not merely ignored — they are rejected with a 422. Ignoring
them would be worse: a caller would believe a parameter had taken effect.

Every one of those values is server-owned because each of them can widen or
redirect the evidence an answer rests on, and that is precisely the property
this product exists to keep fixed.

LIVENESS vs READINESS
---------------------
Liveness answers "is the process running" and must stay cheap and
dependency-free. Readiness answers "can this process serve traffic" and is false
until the corpus is loaded, the index is built and the provider is configured.
Restarting a live-but-not-ready process is wrong; routing traffic to one is
worse.

NO APPLICATION AUTHENTICATION YET
---------------------------------
There is none, and the app binds loopback by default. Adding authentication is
REQUIRED before this is exposed beyond a developer machine — see the README.
It is called out here because an unauthenticated answer endpoint backed by a
metered model is a bill and a data-exposure risk, not merely an open door.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from platform_engineering_assistant.agent.approval import (
    ApprovalError,
    ApprovalRequest,
    ApprovalStatus,
)
from platform_engineering_assistant.agent.domain import AgentRequest, AgentResponse
from platform_engineering_assistant.agent.orchestrator import AgentService, build_agent_service
from platform_engineering_assistant.agent.protocol import AgentDecisionProvider
from platform_engineering_assistant.agent.trajectory import AgentTrajectory
from platform_engineering_assistant.answering import AnsweringService, build_service
from platform_engineering_assistant.domain import AnswerRequest, AnswerResponse
from platform_engineering_assistant.errors import (
    AssistantError,
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    CorpusError,
    FailureCategory,
    InvalidStructuredOutputError,
    NetworkDeniedError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from platform_engineering_assistant.generation.protocol import GenerationProvider
from platform_engineering_assistant.observability import (
    TrajectoryObservability,
    build_observability,
)

REQUEST_ID_HEADER = "x-request-id"


class ApprovalDecisionRequest(BaseModel):
    """A human decision on one approval. Closed, like every request model here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool
    approver: str = Field(min_length=1, max_length=200, description="Identified human principal.")


logger = logging.getLogger("platform_engineering_assistant")

# Status mapping. Chosen so a caller can act: 503 means "try later, the backend
# is unreachable or unauthorised"; 504 means "it was too slow"; 500 means "we
# have a bug". Auth and network failures are 503 rather than 502 because from
# the caller's side the dependency is simply unavailable, and the distinction
# between "we misconfigured RBAC" and "the model is down" is an operator's
# concern, not something to leak into a public error.
_STATUS_BY_CATEGORY: dict[FailureCategory, int] = {
    FailureCategory.RATE_LIMITED: 429,
    FailureCategory.TIMEOUT: 504,
    FailureCategory.AUTHENTICATION: 503,
    FailureCategory.AUTHORIZATION: 503,
    FailureCategory.NETWORK_DENIED: 503,
    FailureCategory.PROVIDER_ERROR: 503,
    FailureCategory.INVALID_STRUCTURED_OUTPUT: 503,
    FailureCategory.CONFIGURATION: 500,
    FailureCategory.CORPUS: 500,
    FailureCategory.RETRIEVAL: 500,
    FailureCategory.UNSUPPORTED_CITATION: 500,
}

# Public error text. Deliberately generic: an error body must not disclose the
# endpoint, the deployment, which role is missing or what the model returned.
_PUBLIC_DETAIL: dict[int, str] = {
    429: "Rate limited. Retry later.",
    500: "Internal error.",
    503: "The answering service is temporarily unavailable.",
    504: "The request timed out.",
}


def create_app(
    provider: GenerationProvider | None = None,
    *,
    service_factory: Callable[[], AnsweringService] | None = None,
    agent_decider: AgentDecisionProvider | None = None,
    trajectory: TrajectoryObservability | None = None,
    environment: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the application.

    `provider`, `service_factory`, `agent_decider` and `trajectory` exist so
    tests can inject deterministic fakes. In production none is supplied and the
    Azure providers and trajectory sinks are built from environment
    configuration at startup.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.service = None
        app.state.agent = None
        app.state.observability = None
        app.state.startup_error = None
        try:
            if service_factory is not None:
                app.state.service = service_factory()
            elif provider is not None:
                app.state.service = build_service(provider)
            else:
                app.state.service = build_service(_default_provider())

            # The agent is built from the SAME answering service, so both routes
            # share one corpus, one index, one prompt and one provider. A second
            # service would be a second answering path.
            decider = agent_decider if agent_decider is not None else _default_decider()
            # Phase 18.5. Trajectory sinks are read from the environment and
            # DEGRADE rather than fail: a log destination that is unavailable
            # must not stop the product serving grounded answers. A dropped sink
            # is logged here so it is visible at startup rather than inferred
            # later from an empty audit trail.
            observability = (
                trajectory
                if trajectory is not None
                else build_observability(environment=environment)
            )
            app.state.observability = observability
            if observability.degraded:
                logger.warning("trajectory sinks degraded: %s", ", ".join(observability.degraded))
            app.state.agent = build_agent_service(
                app.state.service, decider, trajectory_sink=observability.sink
            )
        except AssistantError as error:
            # Readiness stays false and the reason is logged once. The process
            # still starts, so an operator can reach /health/live and see the
            # container is up but deliberately not serving.
            app.state.startup_error = error
            logger.error("startup failed: %s", error)

        yield

        # Stop advertising readiness before shutdown so in-flight rollouts drain
        # rather than race.
        app.state.service = None
        app.state.agent = None
        app.state.observability = None

    app = FastAPI(
        title="Platform Engineering Assistant",
        description=(
            "Grounded question answering over the approved documentation of this "
            "Azure AI platform. Answers cite only retrieved evidence."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 for anything the closed request model rejects.

        The body reports which FIELD was wrong, never the submitted value: a
        question is user content and must not be echoed back into logs or into
        an error a proxy might record.
        """
        fields = sorted(
            {".".join(str(part) for part in error["loc"][1:]) for error in exc.errors()}
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Invalid request.",
                "fields": [field for field in fields if field],
                "request_id": _request_id(request),
            },
        )

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        """Liveness: the process is running. Never touches the corpus or provider."""
        return {"status": "alive"}

    @app.get("/health/ready")
    def health_ready(request: Request) -> Response:
        """Readiness: corpus loaded, index built and provider configured."""
        service: AnsweringService | None = getattr(request.app.state, "service", None)
        if service is None:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(
            status_code=200,
            content={
                "status": "ready",
                "chunks": service.chunk_count,
                "corpus_version": service.corpus_version,
                "prompt_version": service.prompt.version,
                "prompt_hash": service.prompt.short_hash,
                "provider": service.provider_name,
                "agent_prompt_version": (
                    agent.agent_prompt.version if (agent := _agent_of(request)) else None
                ),
                "tools": list(agent.registry.names) if agent else [],
            },
        )

    @app.post("/v1/answers", response_model=AnswerResponse)
    def answers(payload: AnswerRequest, request: Request) -> Response:
        """Answer one question from approved documentation, or refuse."""
        service: AnsweringService | None = getattr(request.app.state, "service", None)
        request_id = _request_id(request)

        if service is None:
            return _error_response(503, request_id)

        try:
            result = service.answer(payload, request_id=request_id)
        except AssistantError as error:
            status = _STATUS_BY_CATEGORY.get(error.category, 500)
            logger.warning(
                "request failed request_id=%s category=%s status=%s",
                request_id,
                error.category,
                status,
            )
            return _error_response(status, request_id, error=error)
        except Exception:
            # Unexpected: log the traceback server-side, disclose nothing.
            logger.exception("unhandled error request_id=%s", request_id)
            return _error_response(500, request_id)

        logger.info("%s", result.telemetry.summary())
        return JSONResponse(
            status_code=200,
            content=result.response.model_dump(mode="json"),
            headers={REQUEST_ID_HEADER: request_id},
        )

    @app.post("/v1/agent", response_model=AgentResponse)
    def agent(payload: AgentRequest, request: Request) -> Response:
        """Run one bounded, policy-controlled agent turn.

        Every outcome below is a NORMAL 200 response, including a denial and an
        approval requirement. They are decisions the application made, not
        errors: mapping `approval_required` to a 4xx would teach callers to
        retry the one thing that must not be retried without a human.
        """
        agent_service: AgentService | None = _agent_of(request)
        request_id = _request_id(request)

        if agent_service is None:
            return _error_response(503, request_id)

        try:
            turn = agent_service.run(payload, request_id=request_id)
        except AssistantError as error:
            status = _STATUS_BY_CATEGORY.get(error.category, 500)
            logger.warning(
                "agent request failed request_id=%s category=%s status=%s",
                request_id,
                error.category,
                status,
            )
            return _error_response(status, request_id, error=error)
        except Exception:
            logger.exception("unhandled agent error request_id=%s", request_id)
            return _error_response(500, request_id)

        logger.info("%s", turn.telemetry.summary())
        return JSONResponse(
            status_code=200,
            content=turn.response.model_dump(mode="json"),
            headers={REQUEST_ID_HEADER: request_id},
        )

    # --- approval surface ---------------------------------------------------
    #
    # Deliberately separate routes from /v1/agent. An approval submitted through
    # the same call that proposed the action would be the agent approving
    # itself with extra steps; separating them is what makes the requester and
    # the approver distinguishable at all.

    @app.get("/v1/approvals")
    def list_approvals(request: Request) -> Response:
        """Pending approvals awaiting a human decision."""
        agent_service = _agent_of(request)
        if agent_service is None:
            return _error_response(503, _request_id(request))
        pending = agent_service.approvals.store.list_pending()
        return JSONResponse(
            status_code=200,
            content={"pending": [item.model_dump(mode="json") for item in pending]},
        )

    @app.get("/v1/approvals/{approval_id}")
    def get_approval(approval_id: str, request: Request) -> Response:
        """One approval record, with expiry evaluated at read time."""
        agent_service = _agent_of(request)
        if agent_service is None:
            return _error_response(503, _request_id(request))
        record = agent_service.approvals.store.get(approval_id)
        if record is None:
            return JSONResponse(status_code=404, content={"detail": "Unknown approval."})
        body = record.model_dump(mode="json")
        body["effective_status"] = record.effective_status().value
        return JSONResponse(status_code=200, content=body)

    @app.get("/v1/trajectories/{trajectory_id}")
    def get_trajectory(trajectory_id: str, request: Request) -> Response:
        """The recorded audit trail of one turn, and its verification verdict.

        Served from the bounded in-memory sink, which is an INSPECTION aid: it
        holds the most recent turns of this process and nothing older. Durable
        retention is the jsonl or log sink, and a caller must not read a 404
        here as evidence that a turn did not happen.

        The response carries identifiers, enums, counts, durations and hashes —
        the same fields the events themselves carry, because there is no other
        kind of field to carry. `defects` is `AgentTrajectory.verify()`: the
        ordering invariants, checked against this record.
        """
        observability: TrajectoryObservability | None = getattr(
            request.app.state, "observability", None
        )
        if observability is None or observability.memory is None:
            return JSONResponse(
                status_code=404,
                content={"detail": "Trajectory inspection is not enabled on this deployment."},
            )

        events = observability.memory.events_for(trajectory_id)
        if not events:
            return JSONResponse(status_code=404, content={"detail": "Unknown trajectory."})

        trajectory_record = AgentTrajectory(trajectory_id=trajectory_id, events=events)
        return JSONResponse(
            status_code=200,
            content={
                "trajectory_id": trajectory_id,
                "events": [event.as_dict() for event in events],
                "well_formed": trajectory_record.is_well_formed,
                "defects": [
                    defect.model_dump(mode="json") for defect in trajectory_record.verify()
                ],
            },
        )

    @app.post("/v1/approvals/{approval_id}/decision", response_model=ApprovalRequest)
    def decide_approval(
        approval_id: str, payload: ApprovalDecisionRequest, request: Request
    ) -> Response:
        """Record a human decision.

        The approver is supplied explicitly and validated: a machine principal,
        or the requester itself, is refused. There is no application user
        authentication yet, so this identifies rather than authenticates — see
        the README. It must be behind real authentication before exposure.
        """
        agent_service = _agent_of(request)
        if agent_service is None:
            return _error_response(503, _request_id(request))
        try:
            decided = agent_service.approvals.decide(
                approval_id, approved=payload.approved, approver=payload.approver
            )
        except ApprovalError as error:
            return JSONResponse(
                status_code=409, content={"detail": "Approval rejected.", "reason": error.reason}
            )
        return JSONResponse(status_code=200, content=decided.model_dump(mode="json"))

    return app


def _agent_of(request: Request) -> AgentService | None:
    return getattr(request.app.state, "agent", None)


def _request_id(request: Request) -> str:
    """Propagate a caller's request id, or mint one.

    A caller-supplied value is length-capped and stripped of anything that is
    not alphanumeric, hyphen or underscore, so it cannot inject a newline into a
    log line or smuggle content through a header.
    """
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    cleaned = "".join(char for char in supplied if char.isalnum() or char in "-_")[:64]
    return cleaned or f"req-{uuid.uuid4().hex[:16]}"


def _error_response(
    status: int, request_id: str, error: AssistantError | None = None
) -> JSONResponse:
    """Build a safe error body, adding Retry-After only when the service told us."""
    headers: dict[str, str] = {REQUEST_ID_HEADER: request_id}

    if status == 429 and isinstance(error, RateLimitedError):
        retry_after = error.retry_after_seconds
        if retry_after is not None and retry_after > 0:
            # Only ever a value the provider supplied; never invented, because a
            # guessed backoff is worse than none.
            headers["Retry-After"] = str(int(retry_after))

    return JSONResponse(
        status_code=status,
        content={
            "detail": _PUBLIC_DETAIL.get(status, "Request failed."),
            "request_id": request_id,
        },
        headers=headers,
    )


def _default_provider() -> GenerationProvider:
    """Build the Azure provider from environment configuration."""
    from platform_engineering_assistant.generation.azure_openai import (
        AzureOpenAIGenerationProvider,
    )
    from platform_engineering_assistant.provider_config import load_provider_config

    return AzureOpenAIGenerationProvider.from_config(load_provider_config())


def _default_decider() -> AgentDecisionProvider:
    """Build the Azure agent decider from environment configuration."""
    from platform_engineering_assistant.agent.protocol import (
        AzureOpenAIAgentDecisionProvider,
    )
    from platform_engineering_assistant.provider_config import load_provider_config

    return AzureOpenAIAgentDecisionProvider.from_config(load_provider_config())


__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "CorpusError",
    "InvalidStructuredOutputError",
    "NetworkDeniedError",
    "ProviderError",
    "REQUEST_ID_HEADER",
    "AgentService",
    "ApprovalDecisionRequest",
    "ApprovalStatus",
    "TimeoutError_",
    "create_app",
]
