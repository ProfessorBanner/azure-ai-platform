"""Choosing the trajectory sinks for a deployment, from the environment.

WHY THE ENVIRONMENT, WHEN RETRIEVAL AND EVALUATION POLICY ARE VERSION-CONTROLLED
--------------------------------------------------------------------------------
Because the kind of decision differs. Retrieval parameters and evaluation
thresholds decide what the product may SAY and what counts as acceptable; they
are version-controlled precisely so no runtime flag can loosen them. Where a log
line is written is an operational property of one deployment — sandbox writes to
a file, an Azure environment writes to the logging stack — and encoding it in
the repository would mean a commit to change a log destination.

Nothing here can change what the agent does, what it records, or what is
redacted. The event schema and its redaction are structural, in
`agent/trajectory.py`, and no setting below can add a field to them.

DEGRADE, NEVER FAIL
-------------------
A misconfigured or unavailable sink is reported and dropped; the remaining
sinks are built and the product serves. This is the one place in the product
where configuration does NOT fail closed, and the asymmetry is deliberate: for
retrieval or credentials, continuing would mean answering without a guarantee,
whereas here continuing means answering without a log line. An agent that
refuses to serve because its telemetry backend is down has converted an
observability outage into an availability one.

The degradation is never silent. `TrajectoryObservability.degraded` names each
sink that was asked for and not built, `/health/ready` is unaffected, and a
warning is logged once at startup.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from platform_engineering_assistant.agent.trajectory import (
    CompositeTrajectorySink,
    InMemoryTrajectorySink,
    JsonLinesTrajectorySink,
    LoggingTrajectorySink,
    NullTrajectorySink,
    TrajectorySink,
)
from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.observability.exporters import (
    ExportingTrajectorySink,
    load_exporter,
)

SINKS_VAR = "AGENT_TRAJECTORY_SINKS"
PATH_VAR = "AGENT_TRAJECTORY_PATH"
CAPACITY_VAR = "AGENT_TRAJECTORY_CAPACITY"
EXPORTER_VAR = "AGENT_TRAJECTORY_EXPORTER"

DEFAULT_CAPACITY = 200
MAX_CAPACITY = 5000

logger = logging.getLogger("platform_engineering_assistant")


class SinkKind(StrEnum):
    """The sinks a deployment may ask for.

    `MEMORY` is what backs the trajectory inspection endpoint. It is bounded and
    process-local, so it is an inspection aid rather than a retention mechanism;
    durable retention is JSONL or the logging stack, which in this platform ends
    up in the Log Analytics workspace the foundation already deploys.
    """

    NONE = "none"
    MEMORY = "memory"
    JSONL = "jsonl"
    LOG = "log"
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    """What was asked for. Validated, but not yet built."""

    sinks: tuple[SinkKind, ...] = (SinkKind.NONE,)
    path: Path | None = None
    capacity: int = DEFAULT_CAPACITY
    exporter: str | None = None

    @property
    def enabled(self) -> bool:
        return any(kind is not SinkKind.NONE for kind in self.sinks)


@dataclass(frozen=True, slots=True)
class TrajectoryObservability:
    """What was actually built, and what could not be."""

    sink: TrajectorySink
    config: ObservabilityConfig
    memory: InMemoryTrajectorySink | None = None
    degraded: tuple[str, ...] = field(default=())

    @property
    def healthy(self) -> bool:
        return not self.degraded


def read_observability_config(
    environment: Mapping[str, str] | None = None,
) -> ObservabilityConfig:
    """Parse the trajectory settings. Rejects nonsense; does not build anything.

    Raises:
        ConfigurationError: for an unknown sink name, a non-numeric or
            out-of-range capacity, or `jsonl` with no path. These are typos in a
            deployment definition, not runtime conditions, and a typo that
            silently disables an audit trail is worse than a startup error.
    """
    env = os.environ if environment is None else environment

    raw = (env.get(SINKS_VAR) or "").strip()
    if not raw:
        return ObservabilityConfig()

    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    kinds: list[SinkKind] = []
    for name in names:
        try:
            kind = SinkKind(name)
        except ValueError as exc:
            allowed = ", ".join(sorted(kind.value for kind in SinkKind))
            raise ConfigurationError(
                f"{SINKS_VAR} names an unknown sink '{name}'. Allowed: {allowed}."
            ) from exc
        if kind not in kinds:
            kinds.append(kind)

    capacity = DEFAULT_CAPACITY
    raw_capacity = (env.get(CAPACITY_VAR) or "").strip()
    if raw_capacity:
        try:
            capacity = int(raw_capacity)
        except ValueError as exc:
            raise ConfigurationError(f"{CAPACITY_VAR} must be a whole number.") from exc
        if not 1 <= capacity <= MAX_CAPACITY:
            raise ConfigurationError(f"{CAPACITY_VAR} must be between 1 and {MAX_CAPACITY}.")

    path: Path | None = None
    raw_path = (env.get(PATH_VAR) or "").strip()
    if raw_path:
        path = Path(raw_path)
    if SinkKind.JSONL in kinds and path is None:
        raise ConfigurationError(f"The jsonl sink requires {PATH_VAR}.")

    exporter = (env.get(EXPORTER_VAR) or "").strip() or None
    if SinkKind.EXPORT in kinds and exporter is None:
        raise ConfigurationError(f"The export sink requires {EXPORTER_VAR}.")

    return ObservabilityConfig(sinks=tuple(kinds), path=path, capacity=capacity, exporter=exporter)


def build_observability(
    config: ObservabilityConfig | None = None,
    environment: Mapping[str, str] | None = None,
) -> TrajectoryObservability:
    """Build the configured sinks, dropping any that cannot be built."""
    resolved = config if config is not None else read_observability_config(environment)

    if not resolved.enabled:
        return TrajectoryObservability(sink=NullTrajectorySink(), config=resolved)

    built: list[TrajectorySink] = []
    memory: InMemoryTrajectorySink | None = None
    degraded: list[str] = []

    for kind in resolved.sinks:
        if kind is SinkKind.NONE:
            continue
        try:
            if kind is SinkKind.MEMORY:
                memory = InMemoryTrajectorySink(capacity=resolved.capacity)
                built.append(memory)
            elif kind is SinkKind.JSONL:
                assert resolved.path is not None  # validated on read
                built.append(JsonLinesTrajectorySink(resolved.path))
            elif kind is SinkKind.LOG:
                built.append(LoggingTrajectorySink())
            elif kind is SinkKind.EXPORT:
                assert resolved.exporter is not None  # validated on read
                built.append(ExportingTrajectorySink(load_exporter(resolved.exporter)))
        except (ConfigurationError, OSError) as error:
            # Named, logged and dropped. The class of the error is recorded, not
            # its message: an exporter's message may quote a URL or a principal.
            degraded.append(kind.value)
            logger.warning(
                "trajectory sink unavailable: sink=%s error=%s",
                kind.value,
                type(error).__name__,
            )

    sink: TrajectorySink = (
        built[0]
        if len(built) == 1
        else (CompositeTrajectorySink(built) if built else NullTrajectorySink())
    )
    return TrajectoryObservability(
        sink=sink, config=resolved, memory=memory, degraded=tuple(degraded)
    )


__all__ = [
    "CAPACITY_VAR",
    "EXPORTER_VAR",
    "PATH_VAR",
    "SINKS_VAR",
    "ObservabilityConfig",
    "SinkKind",
    "TrajectoryObservability",
    "build_observability",
    "read_observability_config",
]
