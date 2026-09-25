"""The Databricks App surface, proven against fakes. No credentials, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ml_platform_operations_agent.adapters.fake import SCENARIOS
from ml_platform_operations_agent.agent import EvidenceSources
from ml_platform_operations_agent.app.responses_agent import (
    MAX_INPUT_ITEMS,
    custom_outputs_for,
    extract_model_name,
    extract_question,
    extract_window_days,
)
from ml_platform_operations_agent.app.server import MAX_BODY_BYTES, create_app
from ml_platform_operations_agent.domain import MAX_QUESTION_CHARS
from ml_platform_operations_agent.errors import InvalidRequestError

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
MODEL = "dev.ml_lifecycle_demo.linear_regression_model"


def client_for(scenario_key: str) -> TestClient:
    s = SCENARIOS[scenario_key]
    return TestClient(
        create_app(EvidenceSources(s.registry, s.run_history, s.monitoring, s.runbooks))
    )


def body(question: str, model: str = MODEL, **custom: Any) -> dict[str, Any]:
    return {
        "input": [{"role": "user", "content": question}],
        "custom_inputs": {"model_name": model, **custom},
    }


# --- health and readiness ---------------------------------------------------


def test_health_answers_without_touching_a_workspace() -> None:
    r = client_for("healthy").get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_reports_read_only_and_no_runtime_model_calls() -> None:
    r = client_for("healthy").get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "read_only": True, "runtime_model_calls": 0}


# --- request parsing --------------------------------------------------------


def test_a_grounded_request_returns_a_diagnosis() -> None:
    r = client_for("supported_drift").post("/responses", json=body("Why did it degrade?"))
    assert r.status_code == 200
    out = r.json()["custom_outputs"]
    assert out["degradation_status"] == "degraded"
    assert out["likely_causes"] and out["likely_causes"][0]["support"] == "supported"
    assert out["read_only"] is True


def test_insufficient_evidence_is_reported_as_such() -> None:
    """The live DEV situation."""
    r = client_for("missing_monitoring_tables").post("/responses", json=body("Why degrade?"))
    assert r.status_code == 200
    out = r.json()["custom_outputs"]
    assert out["degradation_status"] == "insufficient_evidence"
    assert out["likely_causes"] == []
    assert any("monitoring" in item for item in out["limitations"])


def test_a_prohibited_request_is_refused_with_nothing_executed() -> None:
    r = client_for("supported_drift").post(
        "/responses", json=body("Retrain the model and make it Champion.")
    )
    assert r.status_code == 200
    out = r.json()["custom_outputs"]
    assert out["outcome"] == "refused"
    assert out["refusal_reason"] == "state_changing_request"
    assert out["selected_tools"] == []


def test_an_unknown_model_is_refused() -> None:
    r = client_for("healthy").post(
        "/responses", json=body("Why degrade?", "dev.ml_lifecycle_demo.not_a_model")
    )
    assert r.status_code == 200
    assert r.json()["custom_outputs"]["refusal_reason"] == "unknown_model"


def test_the_model_name_is_required_and_never_guessed_from_prose() -> None:
    """Guessing a three-part name out of English is how an agent reads a model
    nobody asked about."""
    r = client_for("healthy").post(
        "/responses", json={"input": [{"role": "user", "content": f"why did {MODEL} degrade?"}]}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        {"input": []},
        {"input": [{"role": "assistant", "content": "hello"}]},
        {"input": [{"role": "user", "content": ""}]},
    ],
)
def test_requests_with_no_user_text_are_rejected(payload: dict[str, Any]) -> None:
    payload["custom_inputs"] = {"model_name": MODEL}
    r = client_for("healthy").post("/responses", json=payload)
    assert r.status_code == 400


def test_non_text_content_is_ignored_rather_than_failing() -> None:
    r = client_for("healthy").post(
        "/responses",
        json={
            "input": [
                {"role": "user", "content": [{"type": "input_image", "image_url": "x"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "Why degrade?"}]},
            ],
            "custom_inputs": {"model_name": MODEL},
        },
    )
    assert r.status_code == 200


def test_a_malformed_payload_is_rejected_without_echoing_it() -> None:
    secret = "SELECT * FROM secrets WHERE token='dapi-SECRET'"
    r = client_for("healthy").post("/responses", content=json.dumps({"input": secret}).encode())
    assert r.status_code in (400, 200)
    assert "dapi-SECRET" not in r.text


def test_an_oversized_body_is_rejected() -> None:
    r = client_for("healthy").post(
        "/responses",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


def test_too_many_input_items_are_rejected() -> None:
    payload = {
        "input": [{"role": "user", "content": "hi"} for _ in range(MAX_INPUT_ITEMS + 1)],
        "custom_inputs": {"model_name": MODEL},
    }
    assert client_for("healthy").post("/responses", json=payload).status_code == 400


def test_an_over_long_question_is_rejected() -> None:
    r = client_for("healthy").post("/responses", json=body("x" * (MAX_QUESTION_CHARS + 1)))
    assert r.status_code == 400


@pytest.mark.parametrize("days", [0, -1, 91, "abc", None])
def test_an_invalid_window_is_rejected(days: Any) -> None:
    r = client_for("healthy").post("/responses", json=body("Why degrade?", MODEL, window_days=days))
    assert r.status_code == 400


# --- extractors -------------------------------------------------------------


def test_extractors_reject_missing_values() -> None:
    from mlflow.types.responses import ResponsesAgentRequest

    req = ResponsesAgentRequest.model_validate(body("Why degrade?"))
    assert extract_question(req) == "Why degrade?"
    assert extract_model_name(req) == MODEL
    assert extract_window_days(req) == 7

    bare = ResponsesAgentRequest.model_validate({"input": [{"role": "user", "content": "q"}]})
    with pytest.raises(InvalidRequestError):
        extract_model_name(bare)


def test_custom_outputs_carry_no_raw_evidence_payload() -> None:
    """Citations carry an address, not the rows behind it."""
    s = SCENARIOS["supported_drift"]
    from ml_platform_operations_agent.adapters.fake import NOW, window
    from ml_platform_operations_agent.agent import DiagnosisRequest, OperationsAgent

    agent = OperationsAgent(EvidenceSources(s.registry, s.run_history, s.monitoring, s.runbooks))
    d = agent.run(
        DiagnosisRequest(
            model_name=s.model_name, window=window(7), observed_at=NOW, question=s.question
        )
    )
    out = custom_outputs_for(d)
    blob = json.dumps(out)
    # A citation NAMES the columns it read (`relevant_fields` legitimately
    # contains "rmse"); what must not appear is the DATA behind them.
    assert "observations" not in blob
    assert "22.0" not in blob and "0.45" not in blob
    for citation in out["supporting_evidence"]:
        assert set(citation) == {
            "source_type",
            "source_identifier",
            "relevant_fields",
            "observed_at",
        }


# --- the read-only guarantee ------------------------------------------------


def test_no_state_changing_route_exists() -> None:
    """The route table is asserted, not the docstring."""
    app = create_app(
        EvidenceSources(
            SCENARIOS["healthy"].registry,
            SCENARIOS["healthy"].run_history,
            SCENARIOS["healthy"].monitoring,
            SCENARIOS["healthy"].runbooks,
        )
    )
    routes = {
        (getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", ()))))
        for r in app.routes
        if hasattr(r, "methods")
    }
    assert routes == {
        ("/health", ("GET",)),
        ("/ready", ("GET",)),
        ("/responses", ("POST",)),
        ("/openapi.json", ("GET", "HEAD")),
    }
    for route_path, methods in routes:
        assert not ({"PUT", "PATCH", "DELETE"} & set(methods)), (
            f"{route_path} exposes a mutation verb"
        )


def test_the_app_makes_no_runtime_model_call() -> None:
    """The agent is deterministic; no provider is reachable from the app path."""
    import ast

    for name in ("responses_agent.py", "server.py"):
        source = (PRODUCT_ROOT / "src/ml_platform_operations_agent/app" / name).read_text()
        tree = ast.parse(source)
        called = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        for forbidden in ("query", "predict_stream", "chat", "completions", "invoke"):
            assert forbidden not in called, f"{name} calls {forbidden}"


def test_tracing_is_disabled_by_default_in_the_app() -> None:
    assert yaml.safe_load((PRODUCT_ROOT / "app.yaml").read_text())["env"]
    env = {
        e["name"]: e.get("value")
        for e in yaml.safe_load((PRODUCT_ROOT / "app.yaml").read_text())["env"]
    }
    assert env["ML_PLATFORM_AGENT_TRACING"] == "disabled"


# --- configuration ----------------------------------------------------------


def test_app_yaml_binds_nothing_and_names_an_explicit_command() -> None:
    cfg = yaml.safe_load((PRODUCT_ROOT / "app.yaml").read_text())
    assert cfg["command"] == ["python", "-m", "ml_platform_operations_agent.app.server"]
    blob = json.dumps(cfg)
    for secret_ish in ("token", "secret", "password", "dapi"):
        assert secret_ish not in blob.lower()


def test_bundle_declares_dev_only_and_no_resources() -> None:
    """No warehouse, no serving endpoint, no secret: declaring an unused
    resource would grant access nothing needs."""
    cfg = yaml.safe_load((PRODUCT_ROOT / "databricks.yml").read_text())
    assert set(cfg["targets"]) == {"dev"}
    app = cfg["targets"]["dev"]["resources"]["apps"]["ml_platform_operations_agent"]
    assert "resources" not in app
    assert len(app["name"]) <= 26
    assert app["name"].replace("-", "").isalnum() and app["name"].islower()


def test_bundle_and_app_yaml_agree_on_the_command() -> None:
    app_yaml = yaml.safe_load((PRODUCT_ROOT / "app.yaml").read_text())
    bundle = yaml.safe_load((PRODUCT_ROOT / "databricks.yml").read_text())
    bundled = bundle["targets"]["dev"]["resources"]["apps"]["ml_platform_operations_agent"]
    assert bundled["config"]["command"] == app_yaml["command"]


def test_requirements_pin_every_runtime_dependency() -> None:
    """Apps supports requirements.txt only — not pyproject or uv.lock — so the
    pins are exported FROM the lock rather than maintained by hand."""
    # `uv export` emits indented "# via ..." provenance comments; strip any
    # line whose first non-space character is a hash, not just column zero.
    lines = [
        line.strip()
        for line in (PRODUCT_ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "requirements.txt is empty"
    unpinned = [line for line in lines if "==" not in line]
    assert not unpinned, f"unpinned requirements: {unpinned}"
    pinned = {line.split("==")[0].lower() for line in lines}
    for required in ("mlflow", "databricks-sdk", "fastapi", "uvicorn", "pydantic"):
        assert required in pinned, f"{required} missing from requirements.txt"


# --- packaging --------------------------------------------------------------

REPO_ROOT = PRODUCT_ROOT.parents[1]


def test_the_vendored_runbook_matches_the_repository_source_of_truth() -> None:
    """The App ships its own copy, and it must not drift.

    Databricks Apps syncs from the BUNDLE directory, and adding a second
    `sync.paths` entry to reach the repository root re-roots the sync and
    breaks every other pattern (observed via `bundle validate`). So the
    approved runbook is vendored here — and duplication without a check is how
    an agent ends up citing a document that no longer says what it quotes.
    """
    vendored = PRODUCT_ROOT / "docs/platform/ml-operations-runbook.md"
    source = REPO_ROOT / "docs/platform/ml-operations-runbook.md"
    assert vendored.is_file(), "the App would ship without its approved runbook"
    assert vendored.read_bytes() == source.read_bytes(), (
        "the vendored runbook has drifted from docs/platform/ml-operations-runbook.md"
    )


def test_every_approved_runbook_is_resolvable_from_the_app_root() -> None:
    """`ML_PLATFORM_REPO_ROOT=.` in app.yaml means the App resolves runbook
    paths against its own deployed tree."""
    from ml_platform_operations_agent.config import RUNBOOK_DOCUMENTS

    for relative in RUNBOOK_DOCUMENTS:
        assert (PRODUCT_ROOT / relative).is_file(), f"{relative} is not in the App source"


def test_the_sync_deny_list_excludes_heavy_and_local_artefacts() -> None:
    """`sync.exclude` is the construct that governs the upload — NOT `include`.

    Proven by a real deploy on 2026-09-04: with an `include` list, the bundle
    uploaded `.venv` (numpy, pyarrow, sklearn), `tests/`, `evaluation/` and
    `pyproject.toml` regardless. `sync.include` ADDS to the default set and
    appears to override the repository `.gitignore`; it restricts nothing.

    The earlier version of this test asserted the opposite and passed, because
    it checked the CONFIG rather than the resulting upload. A test that
    validates an assumption about a tool, without ever observing the tool, is
    worth very little.
    """
    cfg = yaml.safe_load((PRODUCT_ROOT / "databricks.yml").read_text())
    sync = cfg["sync"]
    assert sync["paths"] == ["."], "a second sync path re-roots the sync and breaks the patterns"
    assert "include" not in sync, "include is not an allow-list; it would re-include artefacts"

    excluded = set(sync["exclude"])
    for heavy in (".venv/**", "tests/**", "evaluation/**", "pyproject.toml", "uv.lock"):
        assert heavy in excluded, f"{heavy} would be uploaded to the workspace"


def test_requirements_install_no_development_tooling() -> None:
    """A dev tool in the App runtime is dead weight and extra attack surface."""
    lines = [
        line.strip().split("==")[0].lower()
        for line in (PRODUCT_ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for dev_only in ("pytest", "mypy", "ruff", "types-pyyaml"):
        assert dev_only not in lines, f"{dev_only} would be installed in the App"


def test_requirements_contain_no_editable_or_local_path_installs() -> None:
    """A `-e` or file:// entry cannot install in a clean App container."""
    text = (PRODUCT_ROOT / "requirements.txt").read_text()
    for forbidden in ("-e ", "--editable", "file://", "../", "/Users/"):
        assert forbidden not in text, f"requirements.txt contains {forbidden!r}"


def test_no_second_agent_framework_is_installed() -> None:
    """Matched on the PACKAGE NAME, not a substring of the file: "autogen"
    appears inside uv's own "autogenerated by" header."""
    names = {
        line.strip().split("==")[0].lower()
        for line in (PRODUCT_ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for framework in ("langchain", "langgraph", "openai-agents", "llama-index", "autogen", "mcp"):
        assert framework not in names, f"{framework} is in the App runtime"
