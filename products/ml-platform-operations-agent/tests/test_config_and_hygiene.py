"""Configuration, fixture honesty, and the offline / source-hygiene boundary."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ml_platform_operations_agent import config
from ml_platform_operations_agent.adapters.fake import DEV_SNAPSHOT, SCENARIOS
from ml_platform_operations_agent.domain import TimeWindow
from ml_platform_operations_agent.errors import ConfigurationError

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=NOW - timedelta(days=7), end=NOW)

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PRODUCT_ROOT.parents[1]
#: The CONTROLLED CORE. The offline guarantee is about these files.
CORE_FILES = sorted((PRODUCT_ROOT / "src").rglob("*.py"))

#: The evaluation harness. `scorers.py` and `cli.py` are the AUTHORITATIVE
#: offline gates and must stay MLflow-free — they are what proves the product
#: without a workspace. Only the two `mlflow_*` adapters may import MLflow.
HARNESS_FILES = sorted((PRODUCT_ROOT / "evaluation").rglob("*.py"))

#: Harness modules permitted to import MLflow, by name.
MLFLOW_HARNESS_MODULES = frozenset({"mlflow_scorers.py", "mlflow_runner.py", "mlflow_judge.py"})

SOURCE_FILES = CORE_FILES + HARNESS_FILES

#: Packages this phase must not import. The offline gate is structural: none of
#: these is installed, and this test stops one being added without the
#: architectural decision that should accompany it.
FORBIDDEN_IMPORTS = frozenset(
    {
        "mlflow",
        "databricks",
        "databricks_sdk",
        "databricks_sql",
        "databricks_agents",
        "langgraph",
        "langchain",
        "langchain_core",
        "agents",  # openai-agents
        "openai",
        "mcp",
        "anthropic",
        "requests",
        "httpx",
        "urllib3",
        "fastapi",
        "socket",
    }
)


# --- thresholds must not drift from Phase 15 --------------------------------


def test_thresholds_still_agree_with_the_phase_15_source() -> None:
    """The values are MIRRORED, not imported, so this reads the real file.

    A silent divergence would make every verdict wrong in a way nothing else
    would catch: the agent would judge live metrics against numbers the
    monitoring job does not use.
    """
    source = (REPO_ROOT / config.THRESHOLD_SOURCE).read_text(encoding="utf-8")

    expected = {
        "MONITOR_MAX_RMSE": config.MONITOR_MAX_RMSE,
        "MONITOR_MIN_R2": config.MONITOR_MIN_R2,
        "MAX_FEATURE_DRIFT": config.MAX_FEATURE_DRIFT,
        "MAX_NULL_RATE": config.MAX_NULL_RATE,
    }

    for name, mirrored in expected.items():
        match = re.search(rf"^{name}\s*=\s*([0-9.]+)", source, re.MULTILINE)
        assert match is not None, f"{name} is no longer defined in {config.THRESHOLD_SOURCE}"
        assert float(match.group(1)) == mirrored, (
            f"{name} has drifted: Phase 15 says {match.group(1)}, this product mirrors {mirrored}"
        )


def test_threshold_source_file_exists() -> None:
    assert (REPO_ROOT / config.THRESHOLD_SOURCE).is_file()


def test_every_approved_runbook_exists() -> None:
    for relative in config.RUNBOOK_DOCUMENTS:
        assert (REPO_ROOT / relative).is_file(), f"approved runbook missing: {relative}"


def test_every_threshold_cites_its_definition() -> None:
    for threshold in config.monitoring_thresholds(WINDOW, NOW):
        assert threshold.defined_in.source_identifier == config.THRESHOLD_SOURCE


def test_thresholds_require_an_aware_timestamp() -> None:
    with pytest.raises(ConfigurationError):
        config.monitoring_thresholds(WINDOW, datetime(2026, 9, 3))  # noqa: DTZ001


# --- evidence scope ---------------------------------------------------------


def test_sandbox_is_not_a_governed_catalog() -> None:
    """Sandbox is not a promotion stage; a model there is not one this agent
    should be diagnosing."""
    assert "sandbox" not in config.GOVERNED_CATALOGS
    with pytest.raises(ConfigurationError):
        config.scope_for("sandbox")


def test_scope_refuses_a_table_outside_the_allow_list() -> None:
    scope = config.scope_for("dev")
    with pytest.raises(ConfigurationError):
        scope.table("information_schema.tables")
    with pytest.raises(ConfigurationError):
        scope.table("training_data")


def test_scope_builds_fully_qualified_allow_listed_names() -> None:
    scope = config.scope_for("dev")
    assert scope.table("monitoring_history") == "dev.ml_lifecycle_demo.monitoring_history"
    assert len(scope.allowed_table_names) == len(config.EVIDENCE_TABLES)


@pytest.mark.parametrize(
    "raw",
    [
        "dev.ml_lifecycle_demo.linear_regression_model",
        "prod.ml_lifecycle_demo.linear_regression_model",
    ],
)
def test_governed_model_names_parse(raw: str) -> None:
    assert config.parse_model_name(raw) is not None


@pytest.mark.parametrize(
    "raw",
    [
        "sandbox.ml_lifecycle_demo.m",
        "dev.ml_lifecycle_demo",
        "dev.ml_lifecycle_demo.m.extra",
        "dev.ml_lifecycle_demo.m; DROP TABLE x",
        "dev.`ml`.m",
        "",
    ],
)
def test_ungoverned_or_malformed_names_do_not_parse(raw: str) -> None:
    assert config.parse_model_name(raw) is None


# --- fixture honesty --------------------------------------------------------


def test_every_scenario_is_marked_synthetic() -> None:
    """A fixture must not be able to quietly acquire the authority of a
    measurement."""
    for key, scenario in SCENARIOS.items():
        assert scenario.synthetic is True, f"{key} is not marked synthetic"


def test_all_eighteen_required_scenarios_exist() -> None:
    required = {
        "supported_drift",
        "correlation_only_drift",
        "version_change",
        "healthy",
        "missing_monitoring_tables",
        "incomplete_window",
        "unmeasurable_metrics",
        "conflicting_evidence",
        "unknown_model",
        "historical_alias_unknown",
        "array_predictions",
        "stale_monitoring",
        "injection_in_monitoring",
        "injection_in_runbook",
        "retrain_request",
        "champion_change_request",
        "arbitrary_sql_request",
        "source_failure",
    }
    assert required <= set(SCENARIOS)


def test_dev_snapshot_records_the_real_observation_separately() -> None:
    """The one place real values appear, labelled as such."""
    assert DEV_SNAPSHOT["observed_on"] == "2026-09-03"
    assert DEV_SNAPSHOT["monitoring_tables_present"] is False
    assert DEV_SNAPSHOT["expected_live_status"] == "insufficient_evidence"
    assert DEV_SNAPSHOT["version_count"] == 7


# --- source hygiene: the offline boundary -----------------------------------


def _imported_roots_of_node(node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if node.module and node.level == 0:
        return {node.module.split(".")[0]}
    return set()


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


#: THE CONTROLLED CORE — the modules the offline guarantee is actually about.
#: Named POSITIVELY rather than as "everything except a growing exemption
#: list", because an exemption list quietly becomes the whole package.
#: Every decision the product makes lives in one of these, and none of them may
#: reach a workspace or a model provider.
CONTROLLED_CORE = frozenset(
    {
        "domain.py",
        "config.py",
        "evidence.py",
        "policy.py",
        "tools.py",
        "agent.py",
        "errors.py",
        "protocols.py",
        "fake.py",
        # The live adapter is IN the core: it takes its clients as parameters
        # and imports neither SDK, which is what keeps it testable without
        # credentials.
        "databricks.py",
    }
)

#: The composition root: the only module that CONSTRUCTS SDK clients.
COMPOSITION_ROOT = "smoke.py"

#: The tracing boundary: imports MLflow LAZILY, inside the enabling functions,
#: so the disabled path never loads it. Proven by
#: `test_disabled_tracing_never_loads_mlflow`, which is the property that
#: actually matters — an allow-list entry alone would only be a promise.
TRACING_BOUNDARY = "tracing.py"

#: Modules permitted to name an SDK, each for a stated reason:
#:   smoke.py            constructs clients for the live proof
#:   tracing.py          imports MLflow lazily, behind the mode boundary
#:   responses_agent.py  uses MLflow's genuine ResponsesAgent contract types
#:   server.py           builds the App's live sources from its own identity
SDK_EXEMPT_MODULES = frozenset(
    {COMPOSITION_ROOT, TRACING_BOUNDARY, "responses_agent.py", "server.py"}
)

#: Packages the composition root alone may import.
LIVE_SDK_IMPORTS = frozenset({"mlflow", "databricks"})


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_module_imports_a_forbidden_package(path: Path) -> None:
    """The controlled core is offline BY CONSTRUCTION.

    Live access exists only behind `adapters/protocols.py`, and the SDKs are a
    separate dependency group. This test is what makes "no Databricks or LLM
    call from the core" a property of the code rather than an observation about
    one run.
    """
    forbidden = FORBIDDEN_IMPORTS
    if path.name in SDK_EXEMPT_MODULES or path.name in MLFLOW_HARNESS_MODULES:
        forbidden = FORBIDDEN_IMPORTS - LIVE_SDK_IMPORTS - {"fastapi"}
    offending = _imported_roots(path) & forbidden
    assert not offending, f"{path.name} imports {sorted(offending)}"


@pytest.mark.parametrize(
    "path",
    [p for p in HARNESS_FILES if p.name not in MLFLOW_HARNESS_MODULES],
    ids=lambda p: p.name,
)
def test_the_authoritative_gates_stay_mlflow_free(path: Path) -> None:
    """`evaluation/scorers.py` and `evaluation/cli.py` are what prove this
    product WITHOUT a workspace.

    If MLflow leaked into them, the offline gate would silently depend on the
    thing it exists to be independent of, and a broken MLflow install would
    look like a failing agent.
    """
    assert not (_imported_roots(path) & LIVE_SDK_IMPORTS), f"{path.name} imports an SDK"


def test_the_controlled_core_imports_no_sdk() -> None:
    """The claim, stated over the modules it is actually about.

    Every decision the product makes lives in one of these files. If an SDK
    appears in any of them, a decision has moved next to a workspace call and
    the offline guarantee is gone.
    """
    core = [p for p in CORE_FILES if p.name in CONTROLLED_CORE]
    assert len(core) == len(CONTROLLED_CORE), "the controlled-core file list has drifted"
    for path in core:
        offending = _imported_roots(path) & FORBIDDEN_IMPORTS
        assert not offending, f"{path.name} imports {sorted(offending)}"


def test_only_named_modules_outside_the_core_import_an_sdk() -> None:
    """Asserted positively, so an exemption cannot spread unnoticed."""
    importers = {path.name for path in CORE_FILES if _imported_roots(path) & LIVE_SDK_IMPORTS}
    assert importers <= SDK_EXEMPT_MODULES, f"unexpected SDK importers: {sorted(importers)}"


def test_tracing_imports_mlflow_only_inside_functions() -> None:
    """Module-level would load MLflow for every importer of the package."""
    tracing = PRODUCT_ROOT / "src/ml_platform_operations_agent/tracing.py"
    tree = ast.parse(tracing.read_text(encoding="utf-8"))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            roots = _imported_roots_of_node(node)
            assert not (roots & LIVE_SDK_IMPORTS), "tracing.py imports an SDK at module level"


def test_disabled_tracing_never_loads_mlflow() -> None:
    """The claim, proven by running it rather than by reading the code.

    A subprocess so the assertion is about a clean interpreter, not about
    whatever the test session happened to import earlier.
    """
    import subprocess
    import sys

    script = (
        "import sys;"
        "from ml_platform_operations_agent.agent import OperationsAgent;"
        "from ml_platform_operations_agent.adapters.fake import SCENARIOS;"
        "from ml_platform_operations_agent.agent import DiagnosisRequest, EvidenceSources;"
        "from ml_platform_operations_agent.adapters.fake import NOW, window;"
        "s=SCENARIOS['healthy'];"
        "a=OperationsAgent(EvidenceSources(s.registry,s.run_history,s.monitoring,s.runbooks));"
        "a.run(DiagnosisRequest(model_name=s.model_name, window=window(7), observed_at=NOW,"
        " question=s.question));"
        "print('mlflow' in sys.modules, 'databricks' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=PRODUCT_ROOT,
        check=True,
    )
    assert result.stdout.strip() == "False False", result.stdout


def test_the_live_adapter_module_imports_no_sdk() -> None:
    """It takes clients as parameters. That is what keeps it testable without
    credentials and what keeps the exemption to one file."""
    adapter = PRODUCT_ROOT / "src/ml_platform_operations_agent/adapters/databricks.py"
    assert not (_imported_roots(adapter) & LIVE_SDK_IMPORTS)


def test_no_module_reads_the_wall_clock() -> None:
    """Determinism is a gate. `observed_at` is always supplied by the caller.

    The composition root is exempt: SOMETHING must read a clock once, at the
    boundary, and threading that single value through everything is exactly
    what keeps the agent and adapters reproducible.
    """
    for path in SOURCE_FILES:
        # Exempt: the two REQUEST BOUNDARIES. Something must read a clock once,
        # at the edge, and thread that single value inward — that is exactly
        # what keeps the agent and adapters reproducible. `tracing.py` is
        # exempt from the SDK rule but NOT this one: it has no reason to read a
        # clock, and exempting it "while we are here" is how an exemption list
        # stops meaning anything.
        if path.name in {COMPOSITION_ROOT, "responses_agent.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        stripped = re.sub(r'"""[\s\S]*?"""', "", source)
        stripped = re.sub(r"#.*", "", stripped)
        for forbidden in ("datetime.now(", "datetime.utcnow(", "time.time("):
            assert forbidden not in stripped, f"{path.name} reads the wall clock"


def test_runtime_dependencies_are_minimal() -> None:
    """The dependency list is the enforcement mechanism, not a preference."""
    pyproject = (PRODUCT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    declared = re.findall(r'"([A-Za-z0-9_.-]+)', block)
    assert declared == ["pydantic"], f"unexpected runtime dependencies: {declared}"


def test_no_state_changing_verb_appears_as_a_tool_name() -> None:
    """The read-only claim rests on absent code, not a declining policy."""
    tools_source = (PRODUCT_ROOT / "src/ml_platform_operations_agent/tools.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(tools_source)
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for verb in ("retrain", "promote", "set_alias", "run_job", "execute_sql", "write", "delete"):
        assert not any(verb in name for name in functions), f"a '{verb}' function exists"


def test_no_mlflow_tracking_store_is_left_in_the_repository() -> None:
    """Running the suite must not create `mlruns/` or an MLflow database here.

    A stray tracking store is exactly the artefact this repository already had
    to gitignore once, and it is not gitignored under this product — so it
    would be committable. Asserted rather than trusted: the leak that prompted
    this test came from a test teardown, not from product code.
    """
    for name in ("mlruns", "mlflow.db", "mlartifacts"):
        assert not (PRODUCT_ROOT / name).exists(), f"{name} was created in the product directory"


def test_no_module_imports_databricks_agents() -> None:
    """`databricks-agents` is present in the lock as a TRANSITIVE dependency of
    the `mlflow[databricks]` extra — not something this product added.

    What Phase 19.2 forbids is USING it: `databricks.agents.evals` is the
    legacy MLflow 2 evaluation surface this phase deliberately replaced with
    `mlflow.genai.evaluate`. Presence in a dependency graph is not use, so the
    check is on imports, where the rule actually bites.
    """
    for path in CORE_FILES + HARNESS_FILES:
        source = path.read_text(encoding="utf-8")
        assert "databricks.agents" not in source, f"{path.name} references databricks.agents"
        assert "databricks_agents" not in source, f"{path.name} references databricks_agents"


def test_no_module_uses_the_legacy_mlflow_2_evaluation_api() -> None:
    """`mlflow.evaluate` and `model_type="databricks-agent"` are the MLflow 2
    surface; this phase uses `mlflow.genai.evaluate` throughout."""
    import re

    for path in CORE_FILES + HARNESS_FILES:
        source = path.read_text(encoding="utf-8")
        stripped = re.sub(r'"""[\s\S]*?"""', "", source)
        assert 'model_type="databricks-agent"' not in stripped, f"{path.name}"
        assert not re.search(r"\bmlflow\.evaluate\s*\(", stripped), f"{path.name}"
