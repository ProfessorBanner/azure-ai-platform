"""Test-suite environment. Set before the agent server library is imported.

The Responses server ships OpenTelemetry auto-instrumentation that, left at its
defaults, exports spans to stdout and probes the Azure instance-metadata
endpoint for resource attributes. In a test run that means noise, a wasted
network attempt, and `I/O operation on closed file` when the console exporter
outlives pytest's capture.

Disabling the SDK here keeps the suite offline and quiet. It is a TEST setting
only: the container leaves telemetry alone so Foundry can configure it.
"""

from __future__ import annotations

import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")
os.environ.setdefault("OTEL_METRICS_EXPORTER", "none")
os.environ.setdefault("OTEL_LOGS_EXPORTER", "none")
os.environ.setdefault("OTEL_EXPERIMENTAL_RESOURCE_DETECTORS", "none")
