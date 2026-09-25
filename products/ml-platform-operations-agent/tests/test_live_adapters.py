"""Live adapters, proven WITHOUT credentials.

Every test here injects a fake client. None of them opens a connection, and
none is marked `live` — these run in ordinary CI.

The structural tests are the important ones: they walk the adapter module's AST
and assert that mutating calls are ABSENT. A test that merely observed "no
mutation happened during this run" would prove nothing about the run after it.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ml_platform_operations_agent.adapters.databricks import (
    DatabricksModelRegistry,
    DatabricksMonitoring,
    DatabricksRunHistory,
    RepositoryRunbooks,
    require_profile,
)
from ml_platform_operations_agent.config import scope_for
from ml_platform_operations_agent.domain import (
    EvidenceAbsence,
    ResolvedModel,
    SourceType,
    TimeWindow,
)
from ml_platform_operations_agent.errors import ConfigurationError, EvidenceSourceError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=NOW - timedelta(days=7), end=NOW)
SCOPE = scope_for("dev")
MODEL = "dev.ml_lifecycle_demo.linear_regression_model"

ADAPTER_PATH = (
    Path(__file__).resolve().parents[1] / "src/ml_platform_operations_agent/adapters/databricks.py"
)


# --- structural read-only proof ---------------------------------------------

#: Attribute names that would perform a mutation or start compute. Checked as
#: ATTRIBUTE ACCESS, so `client.jobs.run_now(...)` is caught by `run_now`.
FORBIDDEN_CALLS = frozenset(
    {
        # compute
        "start",
        "execute_statement",
        "get_statement_result_chunk_n",
        # jobs
        "run_now",
        "run_now_and_wait",
        "submit",
        "cancel_run",
        # unity catalog writes
        "create",
        "update",
        "delete",
        "set_alias",
        "delete_alias",
        "create_schema",
        "create_table",
        # mlflow writes
        "log_metric",
        "log_param",
        "log_artifact",
        "set_tag",
        "create_run",
        "create_experiment",
        "set_experiment",
        "register_model",
        "create_model_version",
        "set_registered_model_alias",
        "delete_registered_model_alias",
        "transition_model_version_stage",
        "start_run",
        "log_dict",
        # serving
        "query",
        "predict",
        "invoke",
    }
)


def _attribute_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def test_adapter_module_contains_no_mutating_call() -> None:
    """The read-only claim rests on absent code, checkable without credentials."""
    offending = _attribute_names(ADAPTER_PATH) & FORBIDDEN_CALLS
    assert not offending, f"adapters/databricks.py calls {sorted(offending)}"


def test_adapter_module_never_executes_caller_supplied_sql() -> None:
    source = ADAPTER_PATH.read_text(encoding="utf-8")
    for marker in ("execute_statement", "spark.sql", "statement_execution", "sql("):
        assert marker not in source, f"adapter references {marker}"


def test_adapter_module_never_starts_a_warehouse() -> None:
    source = ADAPTER_PATH.read_text(encoding="utf-8")
    assert "warehouses" not in source
    assert "warehouse_id" not in source


def test_adapter_uses_only_get_for_rest_calls() -> None:
    """Every `api_client.do` call must pass "GET" as a literal first argument."""
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "do"
    ]
    assert calls, "expected at least one REST call to check"
    for call in calls:
        assert call.args, "api_client.do called without a method argument"
        method = call.args[0]
        assert isinstance(method, ast.Constant) and method.value == "GET", (
            "a REST call uses a method other than a literal GET"
        )


def test_adapter_reads_no_wall_clock() -> None:
    """Windows are injected so evidence is reproducible."""
    source = ADAPTER_PATH.read_text(encoding="utf-8")
    stripped = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "datetime.now(" not in stripped


# --- profile handling -------------------------------------------------------


def test_profile_must_be_named_explicitly() -> None:
    """A silent DEFAULT fallback could point a 'DEV proof' at PROD."""
    with pytest.raises(ConfigurationError):
        require_profile({})
    with pytest.raises(ConfigurationError):
        require_profile({"DATABRICKS_CONFIG_PROFILE": "   "})


def test_profile_is_returned_when_set() -> None:
    assert require_profile({"DATABRICKS_CONFIG_PROFILE": "aiplatform-dev"}) == "aiplatform-dev"


# --- fakes ------------------------------------------------------------------


class FakeNotFound(Exception):
    """Named to match the SDK class the adapter recognises."""


FakeNotFound.__name__ = "NotFound"


class FakeApiClient:
    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str]] = []

    def do(self, method: str, path: str) -> Any:
        self.calls.append((method, path))
        if path not in self.payloads:
            raise FakeNotFound(path)
        value = self.payloads[path]
        if isinstance(value, Exception):
            raise value
        return value


class FakeWorkspace:
    def __init__(self, payloads: dict[str, Any], tables: list[str] | None = None) -> None:
        self.api_client = FakeApiClient(payloads)
        self._tables = tables or []

    @property
    def tables(self) -> Any:
        outer = self

        class _Tables:
            def list(self, catalog_name: str, schema_name: str) -> Any:
                return [type("T", (), {"name": n})() for n in outer._tables]

        return _Tables()


#: NOTE THE QUERY STRING. The endpoint returns an EMPTY alias list without
#: `include_aliases=true`, so the fake keys on the full path the adapter must
#: request — a fake that ignored the query string would let the bug back in.
MODEL_PAYLOAD = {
    f"/api/2.1/unity-catalog/models/{MODEL}?include_aliases=true": {
        # Lower-cased, exactly as Unity Catalog returns them.
        "aliases": [
            {"alias_name": "champion", "version_num": 7},
            {"alias_name": "candidate", "version_num": 7},
        ]
    },
    f"/api/2.1/unity-catalog/models/{MODEL}/versions/7": {
        "version": 7,
        "created_at": 1787934684410,
        "status": "READY",
        "run_id": "b931f7114c2e4484831940fd2e7f4569",
        "tags": [
            {"key": "environment", "value": "dev"},
            {"key": "evaluation_rmse", "value": "10.077918"},
        ],
        "model_metrics": [
            {"key": "rmse", "value": 10.077918},
            {"key": "r2", "value": 0.992569},
        ],
    },
}


def registry(payloads: dict[str, Any] | None = None) -> DatabricksModelRegistry:
    return DatabricksModelRegistry(
        client=FakeWorkspace(payloads if payloads is not None else MODEL_PAYLOAD),
        scope=SCOPE,
        window=WINDOW,
    )


# --- registry adapter -------------------------------------------------------


def test_model_resolves_with_aliases_and_provenance() -> None:
    resolved = registry().resolve_model(MODEL)
    assert isinstance(resolved, ResolvedModel)
    assert resolved.full_name == MODEL
    assert len(resolved.versions) == 1
    assert resolved.versions[0].tags["environment"] == "dev"
    assert resolved.versions[0].metrics["rmse"] == pytest.approx(10.077918)


def test_alias_lookup_is_case_insensitive_and_preserves_returned_casing() -> None:
    """UC returns `champion`; Phase 15 writes `Champion`."""
    resolved = registry().resolve_model(MODEL)
    assert isinstance(resolved, ResolvedModel)
    champion = resolved.alias("Champion")
    assert champion is not None
    assert champion.name == "champion"
    assert champion.version == 7


def test_tags_available_reflects_whether_the_key_was_returned() -> None:
    """The CLI omits `tags` entirely; REST returns it. An adapter that could
    not tell the two apart would report provenance as missing."""
    lossy = dict(MODEL_PAYLOAD)
    lossy[f"/api/2.1/unity-catalog/models/{MODEL}/versions/7"] = {
        "version": 7,
        "created_at": 1787934684410,
        "run_id": "b931f7114c2e4484831940fd2e7f4569",
    }
    resolved = registry(lossy).resolve_model(MODEL)
    assert isinstance(resolved, ResolvedModel)
    assert resolved.versions[0].tags_available is False
    assert resolved.versions[0].tags == {}

    full = registry().resolve_model(MODEL)
    assert isinstance(full, ResolvedModel)
    assert full.versions[0].tags_available is True


def test_unknown_model_returns_absence_not_an_error() -> None:
    absent = registry({}).resolve_model(MODEL)
    assert isinstance(absent, EvidenceAbsence)
    assert absent.source_type is SourceType.UNITY_CATALOG_SCHEMA


def test_out_of_scope_model_is_rejected_before_any_call() -> None:
    """An unvalidated name would be interpolated into a REST path."""
    client = FakeWorkspace({})
    adapter = DatabricksModelRegistry(client=client, scope=SCOPE, window=WINDOW)
    result = adapter.resolve_model("prod.ml_lifecycle_demo.linear_regression_model")
    assert isinstance(result, EvidenceAbsence)
    assert client.api_client.calls == []


def test_registry_never_enumerates_the_catalogue() -> None:
    """Only the aliased versions are fetched, never a full listing."""
    adapter = registry()
    adapter.resolve_model(MODEL)
    paths = [path for _m, path in adapter.client.api_client.calls]
    assert paths == [
        f"/api/2.1/unity-catalog/models/{MODEL}?include_aliases=true",
        f"/api/2.1/unity-catalog/models/{MODEL}/versions/7",
    ]


def test_model_request_asks_for_aliases_explicitly() -> None:
    """Without `include_aliases=true` the endpoint returns an empty alias list
    rather than an error, and the agent would lose the record of what is
    serving. Observed live on 2026-09-04."""
    adapter = registry()
    adapter.resolve_model(MODEL)
    model_path = adapter.client.api_client.calls[0][1]
    assert "include_aliases=true" in model_path


def test_transport_failure_is_sanitised() -> None:
    """An SDK error can carry a workspace URL, headers or a response body."""

    class Boom(Exception):
        pass

    secret = "https://adb-1234.azuredatabricks.net token=dapi-SECRET"
    adapter = registry(
        {f"/api/2.1/unity-catalog/models/{MODEL}?include_aliases=true": Boom(secret)}
    )
    with pytest.raises(EvidenceSourceError) as caught:
        adapter.resolve_model(MODEL)
    message = str(caught.value)
    assert secret not in message
    assert "dapi-SECRET" not in message
    assert "adb-1234" not in message
    assert "Boom" in message


# --- run history ------------------------------------------------------------


class FakeMlflow:
    def __init__(self, runs: dict[str, tuple[int, dict[str, float]]]) -> None:
        self.runs = runs
        self.requested: list[str] = []

    def get_run(self, run_id: str) -> Any:
        self.requested.append(run_id)
        start, metrics = self.runs[run_id]
        return type(
            "Run",
            (),
            {
                "info": type("I", (), {"start_time": start})(),
                "data": type("D", (), {"metrics": metrics})(),
            },
        )()


def test_runs_are_reached_only_through_model_versions() -> None:
    """No experiment search: that would read runs from other products."""
    started = int((NOW - timedelta(days=1)).timestamp() * 1000)
    mlflow = FakeMlflow({"b931f7114c2e4484831940fd2e7f4569": (started, {"rmse": 10.077918})})
    history = DatabricksRunHistory(mlflow_client=mlflow, registry=registry())

    runs = history.recent_runs(MODEL, WINDOW)
    assert not isinstance(runs, EvidenceAbsence)
    assert mlflow.requested == ["b931f7114c2e4484831940fd2e7f4569"]
    assert runs[0][1]["rmse"] == pytest.approx(10.077918)


def test_runs_outside_the_window_are_excluded() -> None:
    old = int((NOW - timedelta(days=90)).timestamp() * 1000)
    mlflow = FakeMlflow({"b931f7114c2e4484831940fd2e7f4569": (old, {"rmse": 10.0})})
    history = DatabricksRunHistory(mlflow_client=mlflow, registry=registry())
    assert isinstance(history.recent_runs(MODEL, WINDOW), EvidenceAbsence)


def test_run_failure_is_sanitised() -> None:
    class Explode:
        def get_run(self, run_id: str) -> Any:
            raise RuntimeError("host=https://adb-1234.azuredatabricks.net")

    history = DatabricksRunHistory(mlflow_client=Explode(), registry=registry())
    with pytest.raises(EvidenceSourceError) as caught:
        history.recent_runs(MODEL, WINDOW)
    assert "adb-1234" not in str(caught.value)


# --- monitoring -------------------------------------------------------------


def test_absent_monitoring_table_is_cited_to_the_schema_listing() -> None:
    """Citing the missing table would be a citation to something that does not
    exist — unresolvable, and indistinguishable from a fabricated one."""
    monitoring = DatabricksMonitoring(
        client=FakeWorkspace({}, tables=["training_data", "predictions"]), scope=SCOPE
    )
    result = monitoring.monitoring_history(MODEL, WINDOW)
    assert isinstance(result, EvidenceAbsence)
    assert result.source_type is SourceType.UNITY_CATALOG_SCHEMA
    assert result.source_identifier == "dev.ml_lifecycle_demo"
    assert "not present in this schema's table listing" in result.reason


def test_present_table_still_reports_rows_unavailable_without_compute() -> None:
    """Returning zero rows would read as 'measured and found nothing', which is
    a different and far more dangerous claim than 'not measured'."""
    monitoring = DatabricksMonitoring(
        client=FakeWorkspace({}, tables=["monitoring_history"]), scope=SCOPE
    )
    result = monitoring.monitoring_history(MODEL, WINDOW)
    assert isinstance(result, EvidenceAbsence)
    assert "requires SQL warehouse compute" in result.reason


def test_predictions_never_require_compute() -> None:
    monitoring = DatabricksMonitoring(client=FakeWorkspace({}, tables=[]), scope=SCOPE)
    assert isinstance(monitoring.predictions(MODEL, WINDOW), EvidenceAbsence)


def test_schema_listing_failure_is_sanitised() -> None:
    class Broken:
        @property
        def tables(self) -> Any:
            class _T:
                def list(self, catalog_name: str, schema_name: str) -> Any:
                    raise RuntimeError("PERMISSION_DENIED for principal 1234-abcd")

            return _T()

    monitoring = DatabricksMonitoring(client=Broken(), scope=SCOPE)
    with pytest.raises(EvidenceSourceError) as caught:
        monitoring.monitoring_history(MODEL, WINDOW)
    assert "1234-abcd" not in str(caught.value)


# --- runbooks ---------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]


def runbooks() -> RepositoryRunbooks:
    return RepositoryRunbooks(repository_root=REPO_ROOT, window=WINDOW)


def test_runbook_search_returns_stable_document_and_anchor() -> None:
    hits = runbooks().search("feature drift threshold standardized mean difference")
    assert not isinstance(hits, EvidenceAbsence)
    path, anchor, body = hits[0]
    assert path == "docs/platform/ml-operations-runbook.md"
    assert anchor
    assert body


def test_runbook_search_is_deterministic() -> None:
    """Ties must not depend on filesystem order."""
    first = runbooks().search("drift threshold retrain")
    second = runbooks().search("drift threshold retrain")
    assert first == second


def test_runbook_search_reports_absence_for_an_unmatched_query() -> None:
    result = runbooks().search("quantum chromodynamics tensor")
    assert isinstance(result, EvidenceAbsence)


def test_runbook_adapter_reads_only_allow_listed_documents() -> None:
    adapter = RepositoryRunbooks(
        repository_root=REPO_ROOT, window=WINDOW, documents=("docs/does-not-exist.md",)
    )
    assert isinstance(adapter.search("drift"), EvidenceAbsence)


def test_runbook_adapter_makes_no_network_call() -> None:
    source = ADAPTER_PATH.read_text(encoding="utf-8")
    for marker in ("requests.", "httpx.", "urlopen", "socket."):
        assert marker not in source


# --- absence versus invisibility --------------------------------------------


def test_absence_is_only_claimed_when_the_listing_demonstrably_works() -> None:
    """`tables.list` returns only what the CALLER may see, and Unity Catalog
    does not distinguish "missing" from "not permitted" for a non-privileged
    principal — by design, so existence is not leaked.

    Seeing other tables proves the listing returns content, which is what makes
    a missing monitoring table genuinely missing.
    """
    monitoring = DatabricksMonitoring(
        client=FakeWorkspace({}, tables=["training_data", "predictions"]), scope=SCOPE
    )
    result = monitoring.monitoring_history(MODEL, WINDOW)
    assert isinstance(result, EvidenceAbsence)
    assert result.reason.startswith("table_absent:")
    assert "2 other tables" in result.reason


def test_an_empty_listing_yields_visibility_unverified_not_absence() -> None:
    """If the identity can see NOTHING, absence is not assertable.

    Reporting invisibility as absence would turn a permissions gap into "no
    monitoring exists" — a confident, wrong answer about the one thing this
    agent is for.
    """
    monitoring = DatabricksMonitoring(client=FakeWorkspace({}, tables=[]), scope=SCOPE)
    result = monitoring.monitoring_history(MODEL, WINDOW)
    assert isinstance(result, EvidenceAbsence)
    assert result.reason.startswith("visibility_unverified:")
    assert "lacks permission" in result.reason


def test_visibility_unverified_still_produces_insufficient_evidence() -> None:
    """Either way the agent abstains — but the LIMITATION tells an operator
    which problem they have: no monitoring, or no permission."""
    from ml_platform_operations_agent.agent import (
        DiagnosisRequest,
        EvidenceSources,
        OperationsAgent,
    )
    from ml_platform_operations_agent.domain import DegradationStatus

    blind = DatabricksMonitoring(client=FakeWorkspace({}, tables=[]), scope=SCOPE)
    agent = OperationsAgent(
        EvidenceSources(
            registry_source=registry(),
            run_history=FakeRuns(),
            monitoring=blind,
            runbooks=runbooks(),
        )
    )
    result = agent.run(
        DiagnosisRequest(
            model_name=MODEL, window=WINDOW, observed_at=NOW, question="Why did it degrade?"
        )
    )
    assert result.degradation_status is DegradationStatus.INSUFFICIENT_EVIDENCE
    assert any("visibility_unverified" in item for item in result.limitations)


class FakeRuns:
    def recent_runs(self, full_name: str, window_: Any, limit: int = 20) -> Any:
        return EvidenceAbsence(
            source_type=SourceType.UNITY_CATALOG_MODEL,
            source_identifier=full_name,
            reason="no runs",
            query_window=window_,
        )
