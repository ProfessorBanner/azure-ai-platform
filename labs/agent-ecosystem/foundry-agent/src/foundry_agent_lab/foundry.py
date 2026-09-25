"""The real Microsoft Foundry adapter. Imported only for a live run.

VERIFIED AGAINST THE INSTALLED SDK, NOT AGAINST DOCUMENTATION
-------------------------------------------------------------
`azure-ai-projects==2.5.0`, introspected directly:

  * `AIProjectClient(endpoint=..., credential=...)` — the PROJECT endpoint.
  * `client.agents` -> `AgentsOperations`, carrying `create_version`, `get`,
    `list`, `delete` and the session operations.
  * `models.PromptAgentDefinition(model=..., instructions=..., tools=...)` — the
    declarative agent kind this lab uses.
  * `client.get_openai_client(agent_name=...)` -> a real `openai.OpenAI` bound
    to `{endpoint}/agents/{name}/endpoint/protocols/openai`, authenticated with
    a bearer token from the supplied credential at scope
    `https://ai.azure.com/.default`.

So the conversation itself runs over the OpenAI **Responses API** against the
agent's endpoint. That is worth stating plainly, because it is the single most
surprising thing about the current service: the managed agent supplies the
definition, the versioning and the conversation store, while the turn-by-turn
protocol is the same Responses API the Phase 18 product already speaks.

NOT EXERCISED YET. Phase 19.1b was implemented under an explicit instruction not
to call Azure. The types and call shapes below come from introspecting the
installed SDK; the round trip is unproven until the live command in `smoke.py`
is run. Treat this module as unverified until then.
"""

from __future__ import annotations

import json
from typing import Any

from foundry_agent_lab.config import FoundryLabConfig
from foundry_agent_lab.protocol import ProposedCall, RuntimeResponse


class FoundryAgentRuntime:
    """`AgentRuntime` backed by a Microsoft Foundry managed prompt agent."""

    def __init__(self, config: FoundryLabConfig, client: Any, openai_client: Any) -> None:
        self._config = config
        self._client = client
        self._openai = openai_client

    @classmethod
    def from_config(cls, config: FoundryLabConfig) -> FoundryAgentRuntime:
        """Build a keyless client. Entra ID only, exactly as the product does."""
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        client = AIProjectClient(endpoint=config.project_endpoint, credential=credential)
        openai_client = client.get_openai_client(agent_name=config.agent_name)
        return cls(config, client, openai_client)

    def ensure_agent(self, tool_schemas: list[dict[str, Any]], instructions: str) -> str:
        """Create a new agent VERSION carrying this tool contract.

        A version rather than an edit: the definition that produced a turn stays
        retrievable, which is the managed equivalent of the prompt version and
        hash the product ships with every response.
        """
        from azure.ai.projects.models import FunctionTool, PromptAgentDefinition, Tool

        # The SDK wants typed tool models, not raw dicts. Built from the same
        # schema the lab asserts on, so there is one definition of the contract
        # and the typed form is derived from it rather than written twice.
        tools: list[Tool] = [
            FunctionTool(
                name=schema["name"],
                description=schema["description"],
                parameters=schema["parameters"],
                strict=bool(schema.get("strict", True)),
            )
            for schema in tool_schemas
        ]
        version = self._client.agents.create_version(
            agent_name=self._config.agent_name,
            definition=PromptAgentDefinition(
                model=self._config.model_deployment,
                instructions=instructions,
                tools=tools,
            ),
        )
        return str(getattr(version, "version", "") or getattr(version, "name", ""))

    def start_conversation(self) -> str:
        conversation = self._openai.conversations.create()
        return str(conversation.id)

    @staticmethod
    def _to_response(raw: Any) -> RuntimeResponse:
        """Map a Responses API result onto the lab's own shape.

        Function calls are read from typed output items rather than from any
        free text, so nothing the model writes in prose can be mistaken for a
        call.
        """
        calls: list[ProposedCall] = []
        for item in getattr(raw, "output", []) or []:
            if getattr(item, "type", None) != "function_call":
                continue
            try:
                arguments = json.loads(getattr(item, "arguments", "") or "{}")
            except json.JSONDecodeError:
                # Unparseable arguments are still a proposal; the validation
                # layer refuses them and records why.
                arguments = {"__malformed__": True}
            calls.append(
                ProposedCall(
                    call_id=str(getattr(item, "call_id", "") or getattr(item, "id", "")),
                    name=str(getattr(item, "name", "")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )

        usage = getattr(raw, "usage", None)
        return RuntimeResponse(
            response_id=str(getattr(raw, "id", "")),
            conversation_id=str(getattr(raw, "conversation", None) or ""),
            proposed_calls=tuple(calls),
            output_text=str(getattr(raw, "output_text", "") or ""),
            model=getattr(raw, "model", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )

    def respond(self, conversation_id: str, user_input: str) -> RuntimeResponse:
        raw = self._openai.responses.create(
            conversation=conversation_id,
            input=[{"role": "user", "content": user_input}],
        )
        response = self._to_response(raw)
        return (
            response
            if response.conversation_id
            else RuntimeResponse(**{**vars(response), "conversation_id": conversation_id})
        )

    def submit_tool_outputs(
        self, conversation_id: str, outputs: list[tuple[str, str]]
    ) -> RuntimeResponse:
        raw = self._openai.responses.create(
            conversation=conversation_id,
            input=[
                {"type": "function_call_output", "call_id": call_id, "output": output}
                for call_id, output in outputs
            ],
        )
        response = self._to_response(raw)
        return (
            response
            if response.conversation_id
            else RuntimeResponse(**{**vars(response), "conversation_id": conversation_id})
        )


__all__ = ["FoundryAgentRuntime"]
