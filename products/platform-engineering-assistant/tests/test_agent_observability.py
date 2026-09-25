"""Sink configuration, the exporter seam, and the trajectory inspection route.

The exporter seam is exercised with a fake that satisfies `SpanExporter` and
nothing else. That is the whole point of the seam: an integration that can only
be proven with a vendor package installed is an integration this product's CI
cannot check.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from platform_engineering_assistant.agent.domain import AgentRequest
from platform_engineering_assistant.agent.trajectory import (
    CompositeTrajectorySink,
    InMemoryTrajectorySink,
    JsonLinesTrajectorySink,
    LoggingTrajectorySink,
    NullTrajectorySink,
    TrajectoryEvent,
    TrajectoryEventKind,
    TrajectoryRecorder,
)
from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.observability import (
    ExportingTrajectorySink,
    SinkKind,
    build_observability,
    load_exporter,
    read_observability_config,
)
from platform_engineering_assistant.observability.config import (
    CAPACITY_VAR,
    EXPORTER_VAR,
    PATH_VAR,
    SINKS_VAR,
)
from tests.agent_fakes import agent_service, no_tool, search

# --- 1. reading the configuration -------------------------------------------


def test_no_configuration_means_no_sink() -> None:
    """Observability is opt-in. A deployment that says nothing records nothing."""
    config = read_observability_config({})
    assert config.sinks == (SinkKind.NONE,)
    assert config.enabled is False
    assert isinstance(build_observability(config).sink, NullTrajectorySink)


def test_several_sinks_may_be_configured_together() -> None:
    config = read_observability_config({SINKS_VAR: "memory, log", CAPACITY_VAR: "10"})
    assert config.sinks == (SinkKind.MEMORY, SinkKind.LOG)
    assert config.capacity == 10
    built = build_observability(config)
    assert isinstance(built.sink, CompositeTrajectorySink)
    assert built.memory is not None
    assert built.healthy


def test_a_repeated_sink_is_configured_once() -> None:
    assert read_observability_config({SINKS_VAR: "log,log,log"}).sinks == (SinkKind.LOG,)


def test_an_unknown_sink_name_is_a_configuration_error() -> None:
    """A typo that silently disables an audit trail is worse than a start-up error."""
    with pytest.raises(ConfigurationError) as error:
        read_observability_config({SINKS_VAR: "langsmith"})
    assert "unknown sink" in str(error.value)


def test_the_jsonl_sink_requires_a_path() -> None:
    with pytest.raises(ConfigurationError):
        read_observability_config({SINKS_VAR: "jsonl"})


def test_the_export_sink_requires_an_exporter() -> None:
    with pytest.raises(ConfigurationError):
        read_observability_config({SINKS_VAR: "export"})


@pytest.mark.parametrize("value", ["", "many", "0", "-1", "999999"])
def test_a_meaningless_capacity_is_rejected(value: str) -> None:
    if value == "":
        # An empty value is absence, not an error, and takes the default.
        assert read_observability_config({SINKS_VAR: "memory", CAPACITY_VAR: value}).capacity == 200
        return
    with pytest.raises(ConfigurationError):
        read_observability_config({SINKS_VAR: "memory", CAPACITY_VAR: value})


def test_a_configured_file_sink_is_built(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "trail.jsonl"
    built = build_observability(environment={SINKS_VAR: "jsonl", PATH_VAR: str(path)})
    assert isinstance(built.sink, JsonLinesTrajectorySink)
    assert built.healthy


# --- 2. degrade, never fail --------------------------------------------------


def test_an_unimportable_exporter_degrades_rather_than_failing_startup() -> None:
    """An observability outage must not become an availability outage."""
    built = build_observability(
        environment={
            SINKS_VAR: "memory,export",
            EXPORTER_VAR: "no_such_module_anywhere:build",
        }
    )
    assert built.degraded == ("export",)
    assert built.healthy is False
    # The sink that COULD be built still was.
    assert built.memory is not None


def test_a_degraded_deployment_still_serves_and_still_records_what_it_can() -> None:
    built = build_observability(
        environment={SINKS_VAR: "memory,export", EXPORTER_VAR: "nothing_here:build"}
    )
    service = agent_service(search(), trajectory_sink=built.sink)
    turn = service.run(AgentRequest(question="How is Terraform state separated?"))
    assert turn.response.outcome.value == "answered"
    assert built.memory is not None
    assert built.memory.events_for(turn.response.request_id)


def test_every_sink_failing_leaves_a_working_null_sink() -> None:
    built = build_observability(
        environment={SINKS_VAR: "export", EXPORTER_VAR: "nothing_here:build"}
    )
    assert isinstance(built.sink, NullTrajectorySink)
    assert built.degraded == ("export",)


# --- 3. the exporter seam ----------------------------------------------------


class RecordingExporter:
    """Everything a tracing backend must provide, and nothing else."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, object]]] = []

    def export_span(self, name: str, attributes: dict[str, object]) -> None:
        self.spans.append((name, attributes))


def build_recording_exporter() -> RecordingExporter:
    return RecordingExporter()


def test_an_exporter_receives_one_span_per_event() -> None:
    exporter = RecordingExporter()
    turn = agent_service(search(), trajectory_sink=ExportingTrajectorySink(exporter)).run(
        AgentRequest(question="How is Terraform state separated?")
    )
    assert turn.trajectory is not None
    assert len(exporter.spans) == len(turn.trajectory.events)
    assert exporter.spans[0][0] == "agent.request_received"


def test_exported_spans_carry_the_semantic_convention_names() -> None:
    exporter = RecordingExporter()
    agent_service(search(), trajectory_sink=ExportingTrajectorySink(exporter)).run(
        AgentRequest(question="How is Terraform state separated?")
    )
    tool_spans = [span for span in exporter.spans if span[0] == "agent.tool_call"]
    assert tool_spans
    assert tool_spans[0][1]["gen_ai.tool.name"] == "search_platform_docs"


def test_an_exploding_exporter_never_reaches_the_caller() -> None:
    class ExplodingExporter:
        def export_span(self, name: str, attributes: dict[str, object]) -> None:
            raise RuntimeError("the collector is unreachable")

    sink = ExportingTrajectorySink(ExplodingExporter())
    turn = agent_service(no_tool(), trajectory_sink=sink).run(
        AgentRequest(question="How is Terraform state separated?")
    )
    assert turn.response.outcome.value == "answered"
    assert sink.failures > 0


def test_an_exporter_is_loaded_by_dotted_path() -> None:
    exporter = load_exporter("tests.test_agent_observability:build_recording_exporter")
    assert isinstance(exporter, RecordingExporter)


def test_a_malformed_exporter_path_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        load_exporter("tests.test_agent_observability")


def test_an_object_without_export_span_is_rejected_at_startup() -> None:
    """Discovered at configuration time, not on the first agent turn."""
    with pytest.raises(ConfigurationError) as error:
        load_exporter("tests.test_agent_observability:QUESTION_NOT_AN_EXPORTER")
    assert "export_span" in str(error.value) or "no attribute" in str(error.value)


QUESTION_NOT_AN_EXPORTER = object()


def test_the_configured_exporter_is_wired_end_to_end() -> None:
    built = build_observability(
        environment={
            SINKS_VAR: "export",
            EXPORTER_VAR: "tests.test_agent_observability:build_recording_exporter",
        }
    )
    assert built.healthy
    assert isinstance(built.sink, ExportingTrajectorySink)


# --- 4. no vendor package is required anywhere -------------------------------


def test_no_observability_module_imports_a_vendor_sdk() -> None:
    """The seam is a protocol. Nothing here knows what is on the other side."""
    import pathlib

    root = pathlib.Path("src/platform_engineering_assistant/observability")
    for path in root.glob("*.py"):
        for line in path.read_text().splitlines():
            if line.lstrip().startswith(("import ", "from ")):
                for vendor in ("langsmith", "langchain", "opentelemetry", "azure"):
                    assert vendor not in line, f"{path.name} imports {vendor}"


# --- 5. the inspection route -------------------------------------------------


def app_with_memory_sink() -> TestClient:
    from platform_engineering_assistant.agent.protocol import FakeAgentDecisionProvider
    from platform_engineering_assistant.api.app import create_app
    from platform_engineering_assistant.generation.fake import FakeGenerationProvider

    built = build_observability(environment={SINKS_VAR: "memory"})
    app = create_app(
        FakeGenerationProvider(),
        agent_decider=FakeAgentDecisionProvider([search()]),
        trajectory=built,
    )
    return TestClient(app)


def test_a_turns_trajectory_can_be_read_back_and_verifies_clean() -> None:
    with app_with_memory_sink() as client:
        answered = client.post("/v1/agent", json={"question": "How is Terraform state separated?"})
        request_id = answered.json()["request_id"]

        response = client.get(f"/v1/trajectories/{request_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["well_formed"] is True
        assert body["defects"] == []
        assert [event["kind"] for event in body["events"]][0] == "request_received"
        assert [event["kind"] for event in body["events"]][-1] == "outcome"


def test_an_unknown_trajectory_is_a_404() -> None:
    with app_with_memory_sink() as client:
        assert client.get("/v1/trajectories/agt-nope").status_code == 404


def test_the_route_reports_that_inspection_is_disabled_rather_than_pretending() -> None:
    from platform_engineering_assistant.agent.protocol import FakeAgentDecisionProvider
    from platform_engineering_assistant.api.app import create_app
    from platform_engineering_assistant.generation.fake import FakeGenerationProvider

    app = create_app(
        FakeGenerationProvider(),
        agent_decider=FakeAgentDecisionProvider([no_tool()]),
        environment={},
    )
    with TestClient(app) as client:
        answered = client.post("/v1/agent", json={"question": "How is Terraform state separated?"})
        response = client.get(f"/v1/trajectories/{answered.json()['request_id']}")
        assert response.status_code == 404
        assert "not enabled" in response.json()["detail"]


def test_the_inspection_response_carries_no_question_or_answer_text() -> None:
    marker = "TrajectoryRouteRedactionMarker"
    with app_with_memory_sink() as client:
        answered = client.post("/v1/agent", json={"question": f"What about {marker}?"})
        response = client.get(f"/v1/trajectories/{answered.json()['request_id']}")
        assert marker not in response.text
        assert (answered.json().get("answer") or "ZZZ") not in response.text


# --- 6. the recorder is the only thing that assigns ordering ------------------


def test_the_recorder_numbers_events_and_the_caller_cannot() -> None:
    sink = InMemoryTrajectorySink()
    recorder = TrajectoryRecorder("agt-1", sink)
    for _ in range(3):
        recorder.record(TrajectoryEventKind.STATE_TRANSITION, from_state="a", to_state="b")
    assert [event.sequence for event in sink.events_for("agt-1")] == [1, 2, 3]


def test_the_logging_sink_emits_a_structured_record(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    with caplog.at_level(logging.INFO, logger="platform_engineering_assistant.agent"):
        TrajectoryRecorder("agt-1", LoggingTrajectorySink()).record(
            TrajectoryEventKind.REQUEST_RECEIVED, question_chars=42
        )
    assert any(record.message == "agent_trajectory_event" for record in caplog.records)
    payload = next(
        record.trajectory for record in caplog.records if record.message == "agent_trajectory_event"
    )
    assert payload["question_chars"] == 42
    assert "question" not in payload


def test_an_event_is_immutable_once_recorded() -> None:
    """An audit record that can be edited after the fact is not an audit record."""
    recorder = TrajectoryRecorder("agt-1")
    event = recorder.record(TrajectoryEventKind.REQUEST_RECEIVED)
    with pytest.raises(ValueError):
        event.kind = TrajectoryEventKind.OUTCOME
    assert isinstance(event, TrajectoryEvent)
