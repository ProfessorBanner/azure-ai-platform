"""The live capability command. Manual, human-invoked, never automatic.

    uv run python -m foundry_agent_lab.smoke \
      --question "How is Terraform state separated between environments?"

Calls a metered deployment, so it is not wired into any pipeline and has no
default that reaches the network without being asked. `--offline` runs the same
loop against the deterministic fake, which is what CI exercises.
"""

from __future__ import annotations

import argparse
import sys

from foundry_agent_lab.config import LabConfigurationError, load_config
from foundry_agent_lab.protocol import AgentRuntime, FakeAgentRuntime, RuntimeResponse
from foundry_agent_lab.registry import build_registry
from foundry_agent_lab.runtime import FoundryFunctionLoop, as_json

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIGURATION = 3


def _offline_runtime() -> AgentRuntime:
    """The deterministic fake: propose a search, then answer from its evidence."""
    from foundry_agent_lab.protocol import ProposedCall

    return FakeAgentRuntime(
        scripted=[
            RuntimeResponse(
                response_id="resp-offline-1",
                conversation_id="conv-offline",
                proposed_calls=(
                    ProposedCall(
                        call_id="call-1",
                        name="search_platform_docs",
                        arguments={"query": "terraform state separation environments"},
                    ),
                ),
            ),
            RuntimeResponse(
                response_id="resp-offline-2",
                conversation_id="conv-offline",
                output_text="(offline fake) answered from the retrieved evidence.",
                model="fake-deterministic",
                total_tokens=0,
            ),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="foundry-agent-smoke",
        description="Run one bounded turn against the Foundry managed agent.",
    )
    parser.add_argument("--question", required=True)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the deterministic fake instead of calling Foundry.",
    )
    arguments = parser.parse_args(argv)

    registry = build_registry()

    if arguments.offline:
        runtime: AgentRuntime = _offline_runtime()
    else:
        try:
            config = load_config()
        except LabConfigurationError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return EXIT_CONFIGURATION
        # Imported here so the offline path loads no Azure SDK and cannot
        # accidentally acquire a credential.
        from foundry_agent_lab.foundry import FoundryAgentRuntime

        runtime = FoundryAgentRuntime.from_config(config)

    record = FoundryFunctionLoop(runtime, registry).run(arguments.question)
    print(as_json(record))
    return EXIT_OK if record.final_text else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
