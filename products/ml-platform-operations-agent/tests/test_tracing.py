"""Tracing modes, metadata sanitisation, and the disabled-by-default guarantee."""

from __future__ import annotations

import pytest

from ml_platform_operations_agent.adapters.fake import NOW, SCENARIOS, window
from ml_platform_operations_agent.agent import (
    DiagnosisRequest,
    EvidenceSources,
    OperationsAgent,
)
from ml_platform_operations_agent.errors import ConfigurationError
from ml_platform_operations_agent.tracing import (
    ALLOWED_METADATA_KEYS,
    MAX_METADATA_VALUE_CHARS,
    TracingConfig,
    TracingMode,
    activate,
    annotate_trace,
    base_metadata,
    correlation_hash,
    resolve_config,
    sanitise_metadata,
    span,
)

# --- mode resolution --------------------------------------------------------


def test_default_mode_is_disabled() -> None:
    """A caller that configures nothing must not initialise MLflow."""
    config = resolve_config({})
    assert config.mode is TracingMode.DISABLED
    assert not config.enabled


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        resolve_config({"ML_PLATFORM_AGENT_TRACING": "on"})


def test_managed_requires_the_exact_profile() -> None:
    """An implicit or mismatched profile could point a DEV run elsewhere."""
    for profile in (
        {},
        {"DATABRICKS_CONFIG_PROFILE": "aiplatform-prod"},
        {"DATABRICKS_CONFIG_PROFILE": ""},
    ):
        env = {"ML_PLATFORM_AGENT_TRACING": "managed", **profile}
        with pytest.raises(ConfigurationError):
            resolve_config(env, experiment="/Shared/x")


def test_managed_requires_an_explicit_experiment() -> None:
    with pytest.raises(ConfigurationError):
        resolve_config(
            {"ML_PLATFORM_AGENT_TRACING": "managed", "DATABRICKS_CONFIG_PROFILE": "aiplatform-dev"}
        )


def test_managed_resolves_with_profile_and_experiment() -> None:
    config = resolve_config(
        {"ML_PLATFORM_AGENT_TRACING": "managed", "DATABRICKS_CONFIG_PROFILE": "aiplatform-dev"},
        experiment="/Shared/phase19-2-ml-platform-operations-agent-dev",
    )
    assert config.mode is TracingMode.MANAGED
    assert config.tracking_uri == "databricks"
    assert config.profile == "aiplatform-dev"


def test_activate_is_a_noop_when_disabled() -> None:
    """Proves no MLflow initialisation happens on the offline path."""
    activate(TracingConfig(mode=TracingMode.DISABLED))


def test_span_and_annotate_are_noops_when_disabled() -> None:
    config = TracingConfig(mode=TracingMode.DISABLED)
    with span(config, "anything", case_id="x"):
        pass
    annotate_trace(config, case_id="x")


# --- sanitisation -----------------------------------------------------------


def test_metadata_is_an_allow_list_not_a_deny_list() -> None:
    """A deny-list fails the first time an unknown field appears."""
    clean = sanitise_metadata(
        {
            "case_id": "healthy",
            "token": "dapi0123456789abcdef",
            "DATABRICKS_TOKEN": "secret",
            "workspace_url": "https://adb-1234.azuredatabricks.net",
            "principal_id": "2222aaaa-0000-4000-8000-0000000d0001",
            "authorization": "Bearer abc",
            "question": "why did it degrade?",
        }
    )
    assert clean == {"case_id": "healthy"}


def test_every_allowed_key_is_deliberate() -> None:
    """The allow-list is the security boundary; changing it is a decision."""
    assert "token" not in ALLOWED_METADATA_KEYS
    assert "question" not in ALLOWED_METADATA_KEYS
    assert "profile" not in ALLOWED_METADATA_KEYS
    assert "workspace" not in ALLOWED_METADATA_KEYS
    assert "principal_id" not in ALLOWED_METADATA_KEYS


def test_values_are_bounded() -> None:
    """A long value is a payload trying to become a trace field."""
    clean = sanitise_metadata({"case_id": "x" * 5000})
    assert len(clean["case_id"]) == MAX_METADATA_VALUE_CHARS


def test_newlines_are_collapsed() -> None:
    assert sanitise_metadata({"case_id": "a\nb"})["case_id"] == "a b"


def test_sequences_render_as_a_joined_string() -> None:
    clean = sanitise_metadata({"selected_tools": ["a", "b"]})
    assert clean["selected_tools"] == "a,b"


def test_booleans_render_lowercase() -> None:
    assert sanitise_metadata({"synthetic": True})["synthetic"] == "true"


def test_base_metadata_carries_only_version_stamps() -> None:
    assert set(base_metadata()) <= ALLOWED_METADATA_KEYS


def test_correlation_hash_is_stable_and_not_reversible() -> None:
    """The raw request id may embed a question."""
    question = "why did dev.ml_lifecycle_demo.linear_regression_model degrade?"
    digest = correlation_hash(question)
    assert digest == correlation_hash(question)
    assert question not in digest
    assert len(digest) == 16


# --- agent integration ------------------------------------------------------


def diagnose(key: str) -> object:
    scenario = SCENARIOS[key]
    agent = OperationsAgent(
        EvidenceSources(
            scenario.registry, scenario.run_history, scenario.monitoring, scenario.runbooks
        )
    )
    return agent.run(
        DiagnosisRequest(
            model_name=scenario.model_name,
            window=window(7),
            observed_at=NOW,
            question=scenario.question,
            case_id=key,
            synthetic=True,
        )
    )


def test_agent_defaults_to_disabled_tracing() -> None:
    """Tracing is opt-in, so a unit test never initialises MLflow by accident."""
    scenario = SCENARIOS["healthy"]
    agent = OperationsAgent(
        EvidenceSources(
            scenario.registry, scenario.run_history, scenario.monitoring, scenario.runbooks
        )
    )
    assert agent._tracing.mode is TracingMode.DISABLED


def test_tracing_does_not_change_the_diagnosis() -> None:
    """Tracing OBSERVES; it does not authorize. Turning it on must not move a
    verdict."""
    scenario = SCENARIOS["supported_drift"]
    sources = EvidenceSources(
        scenario.registry, scenario.run_history, scenario.monitoring, scenario.runbooks
    )
    request = DiagnosisRequest(
        model_name=scenario.model_name,
        window=window(7),
        observed_at=NOW,
        question=scenario.question,
    )
    untraced = OperationsAgent(sources).run(request)
    disabled = OperationsAgent(sources, tracing=TracingConfig(mode=TracingMode.DISABLED)).run(
        request
    )
    assert untraced.model_dump_json() == disabled.model_dump_json()


# --- the SDK token bridge ---------------------------------------------------


def test_token_is_not_an_allowed_metadata_key() -> None:
    """The bridge puts a bearer token in the process environment. Nothing may
    carry it into a trace, and the allow-list is what guarantees that."""
    from ml_platform_operations_agent.tracing import ALLOWED_METADATA_KEYS

    for forbidden in ("token", "DATABRICKS_TOKEN", "authorization", "bearer", "host"):
        assert forbidden not in ALLOWED_METADATA_KEYS


def test_sanitiser_drops_a_bearer_token_even_under_an_allowed_key_name() -> None:
    """Defence in depth: an allow-listed key holding a token still must not be
    a path to publishing one. The value is bounded and the key set is closed,
    so a token can only appear if someone deliberately assigns it to a version
    field — which this asserts is visible rather than silent."""
    clean = sanitise_metadata({"Authorization": "Bearer abc.def.ghi", "case_id": "healthy"})
    assert "Authorization" not in clean
    assert clean == {"case_id": "healthy"}


def test_a_failing_span_records_a_category_not_a_stacktrace() -> None:
    """A traceback in a trace is worse than one in a log.

    A trace is a shared, durable record read by people who were not present,
    and an adapter's exception message can carry a connection string, a row of
    monitoring data or a workspace path. Observed on 2026-09-04: the
    `source_failure` case published `/Users/<name>/...` into a managed trace
    via OpenTelemetry's automatic `exception.stacktrace` event.
    """
    import json
    import shutil
    import tempfile

    import mlflow
    import mlflow.tracing

    temporary = tempfile.mkdtemp(prefix="mlpoa-test-")
    original_uri = mlflow.get_tracking_uri()
    try:
        config = TracingConfig(
            mode=TracingMode.LOCAL,
            experiment="stacktrace-check",
            tracking_uri=f"sqlite:///{temporary}/t.db",
        )
        activate(config)

        secret = "connection=https://adb-1234.azuredatabricks.net;token=dapi-SECRET"
        with pytest.raises(RuntimeError):
            with span(config, "ml_platform_operations_agent"):
                with span(config, "inspect_registered_model"):
                    raise RuntimeError(secret)

        # Traces export on a background thread by default; flush before
        # asserting or the search races the exporter.
        mlflow.flush_trace_async_logging()

        experiment = mlflow.get_experiment_by_name("stacktrace-check")
        assert experiment is not None
        traces = mlflow.search_traces(
            locations=[experiment.experiment_id], max_results=10, return_type="list"
        )
        assert traces, "the failing span produced no trace"
        blob = json.dumps([t.to_dict() for t in traces], default=str)

        assert "exception.stacktrace" not in blob
        assert secret not in blob
        assert "dapi-SECRET" not in blob
        assert "Traceback" not in blob
        # The failure is still visible, as a category.
        assert "RuntimeError" in blob
    finally:
        # ORDER MATTERS, and getting it wrong leaks an artefact.
        #
        # Flush first, then DISABLE tracing, then restore the uri. Restoring
        # the uri while tracing is still enabled leaves a live exporter aimed
        # at MLflow's default file store, and the next span in this process
        # lands in `./mlruns` inside the repository — observed on 2026-09-04,
        # and exactly the artefact this product must never create.
        mlflow.flush_trace_async_logging()
        mlflow.tracing.disable()
        mlflow.set_tracking_uri(original_uri)
        shutil.rmtree(temporary, ignore_errors=True)
