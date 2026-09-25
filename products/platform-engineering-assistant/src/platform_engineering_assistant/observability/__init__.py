"""Trajectory observability: where the agent's audit trail is sent.

Deliberately a SEPARATE package from `agent/`. The agent layer defines what an
event is and when one occurs; this package decides where events go. Keeping the
two apart is what lets `tests/test_source_hygiene.py` keep asserting that no
file under `agent/` imports an external observability library — the control flow
that carries the security properties stays free of vendor code.
"""

from platform_engineering_assistant.observability.config import (
    ObservabilityConfig,
    SinkKind,
    TrajectoryObservability,
    build_observability,
    read_observability_config,
)
from platform_engineering_assistant.observability.exporters import (
    ExportingTrajectorySink,
    SpanExporter,
    load_exporter,
)

__all__ = [
    "ExportingTrajectorySink",
    "ObservabilityConfig",
    "SinkKind",
    "SpanExporter",
    "TrajectoryObservability",
    "build_observability",
    "load_exporter",
    "read_observability_config",
]
