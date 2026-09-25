"""The hosted process: the OFFICIAL Foundry Responses server over the Phase 18 agent.

    azure-ai-agentserver-responses   protocol, SSE, response store, readiness
        |
        v  @host.response_handler
    this module                      translation ONLY
        |
        v  AgentService.run()
    Phase 18                         registry, policy, bounded loop, approval, audit

WHAT THE LIBRARY OWNS NOW
-------------------------
`/responses` and its lifecycle, `/responses/{id}`, cancel, input items, and
`/readiness`. None of that is hand-rolled any more. The handler contract is
exact and enforced at decoration time: `async def (request, context,
cancellation_signal)`, three positional parameters, in that order.

WHY THE TURN RUNS IN A THREAD
-----------------------------
`AgentService.run` is synchronous and makes blocking model calls. Awaiting it
directly on the event loop would stall every other in-flight request in the
process, which for a hosted agent means stalling Foundry's own routing. It is
offloaded so concurrency is the server's to manage.

APPROVAL AND AUDIT REMAIN APPLICATION-OWNED
-------------------------------------------
The approval routes are mounted on this server, served from the product's own
`ApprovalService`. Foundry hosts the process; it does not hold the approval
record and cannot decide one. That boundary is the point of the whole
experiment, and adopting the official protocol server did not move it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any, cast

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseObject,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    TextResponse,
)
from azure.ai.agentserver.responses.models._generated.types import Metadata
from platform_engineering_assistant.agent.domain import AgentRequest
from platform_engineering_assistant.errors import AssistantError

from hosted_agent.config import HostedAgentConfig
from hosted_agent.protocol import metadata_for, text_for

logger = logging.getLogger("hosted_agent")


def _as_metadata(values: dict[str, str]) -> Metadata:
    """Present a plain string map as the library's `Metadata` type.

    `Metadata` is a TypedDict declaring no keys — the generated marker for an
    arbitrary string map, and at runtime just a dict. The cast states that
    intent for the type checker rather than suppressing the error blindly.
    """
    return cast(Metadata, values)


def build_agent() -> Any:
    """Build the Phase 18 agent from environment configuration.

    Called ONCE, before the server starts serving. A hosted process that built
    its corpus lazily would report ready — the library owns `/readiness` — while
    still unable to answer, so failure has to happen before the server is up.
    """
    from platform_engineering_assistant.agent.orchestrator import build_agent_service
    from platform_engineering_assistant.agent.protocol import AzureOpenAIAgentDecisionProvider
    from platform_engineering_assistant.answering import build_service
    from platform_engineering_assistant.generation.azure_openai import (
        AzureOpenAIGenerationProvider,
    )
    from platform_engineering_assistant.observability import build_observability
    from platform_engineering_assistant.provider_config import load_provider_config

    provider_config = load_provider_config()
    answering = build_service(AzureOpenAIGenerationProvider.from_config(provider_config))
    observability = build_observability()
    return build_agent_service(
        answering,
        AzureOpenAIAgentDecisionProvider.from_config(provider_config),
        trajectory_sink=observability.sink,
    )


def build_host(
    config: HostedAgentConfig,
    agent_factory: Callable[[], Any] = build_agent,
) -> ResponsesAgentServerHost:
    """Assemble the official server around the Phase 18 agent.

    `agent_factory` exists so tests can inject the deterministic fake; in the
    container it is absent and the Azure providers are built from environment
    configuration.
    """
    agent = agent_factory()
    host = ResponsesAgentServerHost(options=ResponsesServerOptions(default_model=config.agent_name))

    @host.response_handler
    async def handle(
        request: CreateResponse,
        context: ResponseContext,
        cancellation_signal: asyncio.Event,
    ) -> TextResponse:
        """One bounded Phase 18 turn, rendered as a Responses stream."""
        request_id = f"req-{uuid.uuid4().hex[:16]}"
        question = (await context.get_input_text()).strip()

        if not question:
            # A refusal, not a crash: the protocol server turns an exception
            # into a failed response, and "you sent nothing" is not a fault of
            # the agent.
            return TextResponse(
                context,
                request,
                text="No question was supplied.",
                configure=lambda obj: obj.update(
                    {"metadata": _as_metadata({"outcome": "empty_request"})}
                ),
            )
        if len(question) > config.max_input_chars:
            return TextResponse(
                context,
                request,
                text="The request exceeds the configured maximum length.",
                configure=lambda obj: obj.update(
                    {"metadata": _as_metadata({"outcome": "input_too_long"})}
                ),
            )

        try:
            # Offloaded: a blocking model call must not stall the event loop.
            turn = await asyncio.to_thread(agent.run, AgentRequest(question=question), request_id)
        except AssistantError as error:
            # A KNOWN failure. The product classifies every provider and
            # configuration fault into a category, so the caller gets a named
            # outcome rather than a stack trace.
            #
            # Bound now, not in the lambda: `except ... as` unbinds the name at
            # the end of the block, and `configure` is called later.
            failure = str(error.category)
            logger.warning("hosted turn failed request_id=%s category=%s", request_id, failure)
            return TextResponse(
                context,
                request,
                text="The request could not be completed.",
                configure=lambda obj: obj.update(
                    {
                        "metadata": _as_metadata(
                            {
                                "request_id": request_id,
                                "outcome": "failed",
                                "failure_category": failure,
                            }
                        )
                    }
                ),
            )
        except Exception:
            # AN UNEXPECTED DEFECT. Without this the exception escapes into the
            # protocol server, which renders an opaque
            # `{"code": "server_error"}` with nothing to correlate against —
            # which is exactly what 19.1e spent an afternoon failing to
            # diagnose. `AgentService.run` documents that it raises only
            # `AssistantError`, so reaching here means a genuine bug, and a bug
            # that cannot be traced is worse than one that can.
            #
            # The TRACEBACK GOES TO THE LOG, NEVER TO THE CALLER. A traceback
            # carries file paths, and an exception message can quote the
            # request, the corpus or a principal. The caller gets the
            # correlation id and nothing else; an operator joins the two.
            logger.exception("hosted turn raised an unexpected error request_id=%s", request_id)
            return TextResponse(
                context,
                request,
                text=(
                    "The request could not be completed because of an internal error. "
                    f"Quote request_id={request_id} when reporting it."
                ),
                configure=lambda obj: obj.update(
                    {
                        "metadata": _as_metadata(
                            {
                                "request_id": request_id,
                                "outcome": "failed",
                                "failure_category": "internal_error",
                            }
                        )
                    }
                ),
            )

        logger.info("%s", turn.telemetry.summary())
        response = turn.response

        def configure(obj: ResponseObject) -> None:
            """Carry the structured outcome onto the protocol object."""
            obj.update({"metadata": _as_metadata(metadata_for(response))})

        return TextResponse(context, request, text=text_for(response), configure=configure)

    _mount_approval_routes(host, agent)
    return host


def _mount_approval_routes(host: ResponsesAgentServerHost, agent: Any) -> None:
    """The application-owned approval surface, served by this process."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def list_approvals(request: Request) -> JSONResponse:
        pending = agent.approvals.store.list_pending()
        return JSONResponse({"pending": [item.model_dump(mode="json") for item in pending]})

    async def get_approval(request: Request) -> JSONResponse:
        approval_id = request.path_params["approval_id"]
        record = agent.approvals.store.get(approval_id)
        if record is None:
            return JSONResponse({"detail": "Unknown approval."}, status_code=404)
        body = record.model_dump(mode="json")
        body["effective_status"] = record.effective_status().value
        return JSONResponse(body)

    host.add_route("/v1/approvals", list_approvals, methods=["GET"])
    host.add_route("/v1/approvals/{approval_id}", get_approval, methods=["GET"])


__all__ = ["build_agent", "build_host"]
