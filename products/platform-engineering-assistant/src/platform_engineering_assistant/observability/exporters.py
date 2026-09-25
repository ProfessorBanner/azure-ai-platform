"""The one seam through which an external tracing backend may see a trajectory.

WHY A SEAM AND NOT A DEPENDENCY
-------------------------------
The Phase 18.5 brief asked whether LangSmith earns a place as an optional
observability layer, and whether Azure AI Foundry tracing adds something
complementary. The assessment is recorded in
`docs/adr/0009-agent-observability-and-audit-trail.md`; what it produced is this
module rather than a package in `pyproject.toml`.

The reasoning, in short. Both candidates want the same thing from us — an
ordered stream of attributed spans — and neither can be given the thing that
makes their product interesting, because this product does not COLLECT prompts,
answers, tool arguments or reasoning. What we can export is identifiers, enums,
counts, durations and hashes. A backend receiving only that is a span viewer,
and the cost of one is a vendor SDK plus, for LangSmith, an API key: a
long-lived credential, which this platform's rules forbid outright, and
third-party egress of agent metadata, which is a data decision rather than a
library choice.

So the integration is real but bounded: this module defines the protocol, the
mapping and the failure behaviour, and a deployment supplies the object. The
adapter for a given backend is ten lines written against `SpanExporter`, and
nothing here — or anywhere else in the product — imports a vendor package.

`TrajectoryEvent.as_otel_attributes()` does the mapping, and it maps onto the
OpenTelemetry GenAI semantic conventions rather than onto any vendor's schema.
That is the deliberate choice: Foundry tracing IS those conventions over Azure
Monitor, LangSmith ingests OTel, and a convention outlives the backend that
happens to be receiving it today.
"""

from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

from platform_engineering_assistant.agent.trajectory import TrajectoryEvent
from platform_engineering_assistant.errors import ConfigurationError


@runtime_checkable
class SpanExporter(Protocol):
    """What a tracing backend must provide. One method, no vendor types.

    `attributes` is a flat mapping of scalars under OpenTelemetry GenAI
    convention names. An implementation is expected to be cheap and
    non-throwing; if it throws anyway, `ExportingTrajectorySink` contains it.
    """

    def export_span(self, name: str, attributes: dict[str, object]) -> None: ...


class ExportingTrajectorySink:
    """Adapts trajectory events onto a `SpanExporter`.

    Every event becomes one span named for its kind. Nesting is deliberately not
    attempted: a parent/child tree would have to be reconstructed from the
    event stream, and a reconstructed tree that is subtly wrong is worse for an
    audit than a flat, ordered list that is exactly right. The sequence number
    is exported, so a backend that wants ordering has it.
    """

    def __init__(self, exporter: SpanExporter, *, prefix: str = "agent") -> None:
        self._exporter = exporter
        self._prefix = prefix
        self._failures = 0

    @property
    def failures(self) -> int:
        return self._failures

    def emit(self, event: TrajectoryEvent) -> None:
        try:
            self._exporter.export_span(
                f"{self._prefix}.{event.kind.value}", event.as_otel_attributes()
            )
        except Exception:  # noqa: BLE001 - an exporter must never fail a turn
            self._failures += 1


def load_exporter(dotted_path: str) -> SpanExporter:
    """Resolve `package.module:factory` and call it to obtain a `SpanExporter`.

    Imported by name at runtime rather than at module scope, so the product
    neither declares nor loads a vendor package unless a deployment has asked
    for one. The returned object is checked against the protocol here, at
    startup, rather than discovered to be wrong on the first agent turn.

    Raises:
        ConfigurationError: when the path is malformed, cannot be imported, or
            does not produce something satisfying `SpanExporter`.
    """
    if ":" not in dotted_path:
        raise ConfigurationError("A trajectory exporter must be given as 'package.module:factory'.")
    module_name, _, attribute = dotted_path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"The configured trajectory exporter module could not be imported: {module_name}"
        ) from exc

    factory = getattr(module, attribute, None)
    if factory is None:
        raise ConfigurationError(
            f"The configured trajectory exporter has no attribute '{attribute}'."
        )

    exporter = factory() if callable(factory) else factory
    if not isinstance(exporter, SpanExporter):
        raise ConfigurationError(
            "The configured trajectory exporter does not provide export_span(name, attributes)."
        )
    return exporter


__all__ = ["ExportingTrajectorySink", "SpanExporter", "load_exporter"]
