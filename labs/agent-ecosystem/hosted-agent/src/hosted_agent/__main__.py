"""Container entry point. `python -m hosted_agent`.

Named in `CodeConfiguration.entry_point` and as the image CMD. Binds the port
Foundry supplies via `PORT`, defaulting to 8088.

STARTUP FAILURE IS FATAL, DELIBERATELY. The protocol library owns `/readiness`,
so a process that started with a broken corpus would report ready and then fail
every turn. Building the agent before the server starts means a configuration
problem stops the container instead, which is the signal Foundry can act on.
"""

from __future__ import annotations

import logging
import sys

from hosted_agent.config import HostedConfigurationError, load_hosted_config


def main() -> int:
    logging.basicConfig(level=logging.INFO)

    from platform_engineering_assistant.errors import AssistantError

    from hosted_agent.server import build_host

    try:
        config = load_hosted_config()
    except HostedConfigurationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 3

    try:
        host = build_host(config)
    except AssistantError as error:
        print(f"Agent could not be built: category={error.category}", file=sys.stderr)
        return 3

    host.run(host="0.0.0.0", port=config.port)  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
