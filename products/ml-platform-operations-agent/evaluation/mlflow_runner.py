"""The MLflow 3 GenAI evaluation runner.

    # plan only, creates nothing:
    uv run --group live python -m evaluation.mlflow_runner plan

    # local instrumentation check, temp dir outside the repository:
    uv run --group live python -m evaluation.mlflow_runner local

    # the authorised managed run:
    DATABRICKS_CONFIG_PROFILE=aiplatform-dev \\
    MLFLOW_GENAI_EVAL_MAX_WORKERS=1 \\
      uv run --group live python -m evaluation.mlflow_runner managed

THE SAME 19 CASES, NOT A SECOND DATASET
----------------------------------------
`evaluation/cases.jsonl` is read directly. No managed dataset is uploaded and
no copy is made: two datasets drift, and the drifted one is always the one that
gets published.

CONCURRENCY
-----------
`mlflow.genai.evaluate` in 3.16.0 takes no `max_concurrency` argument — the
signature is `(data, scorers, predict_fn, model_id)`. Concurrency is controlled
by the `MLFLOW_GENAI_EVAL_MAX_WORKERS` environment variable (default 10), which
`managed` asserts is `1` rather than setting silently: an operator running this
should see the setting in the command they typed.

DETERMINISM IS CHECKED IN THE TARGET
-------------------------------------
A scorer receives one output and cannot re-run anything, so the target runs
each case twice and compares the serialised answers. The verdict travels in
`expectations["deterministic"]` and `deterministic_output` surfaces it.

PROVENANCE IS HONEST ABOUT THE DIRTY TREE
------------------------------------------
The working tree is uncommitted, so a bare commit SHA would be a false claim:
the code that ran is not the code at that commit. Both facts are recorded — the
base commit AND `working_tree_dirty=true` — plus a content hash of the source
actually executed, which is the only value that identifies what really ran.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from evaluation.cli import load_cases
from evaluation.mlflow_scorers import ALL_SCORERS
from ml_platform_operations_agent.adapters.fake import NOW, SCENARIOS, window
from ml_platform_operations_agent.agent import (
    DiagnosisRequest,
    EvidenceSources,
    OperationsAgent,
)
from ml_platform_operations_agent.tracing import (
    TracingConfig,
    TracingMode,
    activate,
    base_metadata,
    bridged_credentials,
    resolve_config,
)

EXPERIMENT = "/Shared/phase19-2-ml-platform-operations-agent-dev"
MAX_WORKERS_VAR = "MLFLOW_GENAI_EVAL_MAX_WORKERS"

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (PRODUCT_ROOT / "src", PRODUCT_ROOT / "evaluation")

#: Set by `main` before the target runs, so the target can emit spans without
#: threading a config through the MLflow-supplied call signature.
_TRACING: TracingConfig = TracingConfig(mode=TracingMode.DISABLED)


def source_hash() -> str:
    """A content hash of the source that actually ran.

    The only honest identifier for an uncommitted tree. Sorted so it is stable
    across filesystems.
    """
    digest = hashlib.sha256()
    for directory in SOURCE_DIRS:
        for path in sorted(directory.rglob("*.py")):
            digest.update(path.relative_to(PRODUCT_ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def git_provenance() -> dict[str, str]:
    """Base commit plus an explicit dirty marker. Never a bare SHA alone."""

    def _git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=PRODUCT_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except Exception:
            return ""

    dirty = bool(_git("status", "--porcelain"))
    return {
        "base_commit": _git("rev-parse", "--short", "HEAD") or "unknown",
        "working_tree_dirty": "true" if dirty else "false",
        "source_hash": source_hash(),
    }


def _agent_for(scenario_key: str) -> OperationsAgent:
    scenario = SCENARIOS[scenario_key]
    return OperationsAgent(
        EvidenceSources(
            registry_source=scenario.registry,
            run_history=scenario.run_history,
            monitoring=scenario.monitoring,
            runbooks=scenario.runbooks,
        ),
        tracing=_TRACING,
    )


def predict(case_id: str, scenario: str, question: str, window_days: int) -> dict[str, Any]:
    """The evaluation target.

    MLflow unpacks `inputs` as KEYWORD ARGUMENTS, so the parameter names here
    must match the `inputs` keys exactly — it is not called with a dict.

    Fake adapters only. No live Databricks call and no model call: this target
    exists to exercise the controlled core against labelled synthetic evidence.
    """
    request = DiagnosisRequest(
        model_name=SCENARIOS[scenario].model_name,
        window=window(window_days),
        observed_at=NOW,
        question=question,
        case_id=case_id,
        synthetic=True,
    )
    diagnosis = _agent_for(scenario).run(request)
    return {"diagnosis": diagnosis.model_dump(mode="json")}


def build_data(cases: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """The in-memory dataset. Nested `inputs`/`expectations`, as MLflow requires.

    The determinism check runs HERE, once per case, because a scorer cannot
    re-run the target. Untraced: it is a property of the run, not a second
    answer worth storing.
    """
    rows: list[dict[str, Any]] = []
    for case in cases:
        scenario = SCENARIOS[case["scenario"]]
        question = case.get("question") or scenario.question
        days = int(case.get("window_days", 7))

        first = _plain_run(case["scenario"], question, days)
        second = _plain_run(case["scenario"], question, days)

        # NONE-VALUED EXPECTATIONS ARE DROPPED, NOT COERCED.
        #
        # MLflow converts each expectation into an `Expectation` assessment and
        # raises "The `value` field must be specified" on None. Dropping the key
        # is semantically identical here because every gate reads expectations
        # with `.get(...)`, which returns None for a missing key too — so
        # `{"resolved_model": None}` and an absent key mean the same thing to
        # the scorer. Coercing to a string sentinel would NOT be identical: it
        # would make the gate compare against "None".
        expectations = {k: v for k, v in case.get("expected", {}).items() if v is not None}
        expectations["deterministic"] = first == second

        rows.append(
            {
                "inputs": {
                    "case_id": case["id"],
                    "scenario": case["scenario"],
                    "question": question,
                    "window_days": days,
                },
                "expectations": expectations,
            }
        )
    return rows


def _plain_run(scenario: str, question: str, days: int) -> str:
    """Untraced execution used only for the determinism comparison."""
    scenario_obj = SCENARIOS[scenario]
    agent = OperationsAgent(
        EvidenceSources(
            registry_source=scenario_obj.registry,
            run_history=scenario_obj.run_history,
            monitoring=scenario_obj.monitoring,
            runbooks=scenario_obj.runbooks,
        )
    )
    return agent.run(
        DiagnosisRequest(
            model_name=scenario_obj.model_name,
            window=window(days),
            observed_at=NOW,
            question=question,
        )
    ).model_dump_json()


def plan(cases: tuple[dict[str, Any], ...], mode: str) -> dict[str, Any]:
    """What a managed run WOULD do. Creates and initialises nothing."""
    return {
        "mode": mode,
        "tracking_uri": "databricks",
        "profile": os.environ.get("DATABRICKS_CONFIG_PROFILE") or "(not set)",
        "experiment": EXPERIMENT,
        "case_count": len(cases),
        "scorer_names": [s.name for s in ALL_SCORERS],
        "scorer_count": len(ALL_SCORERS),
        "expected_root_traces": len(cases),
        "expected_model_calls": 0,
        "llm_judges": 0,
        "warehouse_activity": "none — no SQL warehouse is started or queried",
        "max_workers_env": os.environ.get(MAX_WORKERS_VAR) or "(unset, MLflow default 10)",
        "permitted_remote_records": [
            "one MLflow experiment (created if absent)",
            "one evaluation run",
            f"{len(cases)} root traces with nested agent/tool spans",
            "deterministic scorer assessments on those traces",
        ],
        "will_not_create": [
            "evaluation dataset",
            "labeling session",
            "review app",
            "scheduled scorers",
            "online monitoring",
            "registered model",
            "remote prompt",
        ],
        "evidence": "synthetic",
        **git_provenance(),
    }


@contextlib.contextmanager
def bridged() -> Iterator[None]:
    """Bridge credentials for `managed` only. `local` needs none."""
    if _TRACING.mode is TracingMode.MANAGED:
        with bridged_credentials(_TRACING):
            yield
    else:
        yield


def run_evaluation(mode: str, cases: tuple[dict[str, Any], ...]) -> Any:
    """Execute one evaluation. `mlflow` is imported here, never at module import."""
    global _TRACING

    temporary: str | None = None

    if mode == "local":
        # A temp dir OUTSIDE the repository. `mlruns/` in the working tree is
        # exactly the artefact this repository already had to gitignore once.
        #
        # SQLITE, NOT A FILE STORE. MLflow 3.16 raises on the filesystem
        # backend outright — "in maintenance mode ... migrate to a database
        # backend". `MLFLOW_ALLOW_FILE_STORE=true` would opt out, but opting
        # out of a deprecation to keep a test green is how a test starts
        # exercising a path nothing else uses.
        temporary = tempfile.mkdtemp(prefix="mlpoa-trace-")
        _TRACING = TracingConfig(
            mode=TracingMode.LOCAL,
            experiment="phase19-2d-local",
            tracking_uri=f"sqlite:///{temporary}/tracing.db",
        )
    else:
        _TRACING = resolve_config(experiment=EXPERIMENT)
        workers = os.environ.get(MAX_WORKERS_VAR)
        if workers != "1":
            raise SystemExit(
                f"{MAX_WORKERS_VAR} must be set to 1 for the managed run; "
                f"it is {workers!r}. Concurrency is not a parameter of "
                "mlflow.genai.evaluate in 3.16.0."
            )

    try:
        # The credential is scoped to the evaluation and no longer.
        with bridged():
            return _evaluate_within(cases)
    finally:
        if temporary is not None:
            # Removed on the way out, on success OR failure: the local mode
            # exists to prove the instrumentation runs, not to leave a
            # tracking store behind.
            shutil.rmtree(temporary, ignore_errors=True)


def _evaluate_within(cases: tuple[dict[str, Any], ...]) -> Any:
    import mlflow

    activate(_TRACING)

    data = build_data(cases)
    provenance = {**base_metadata(), **git_provenance()}

    with mlflow.start_run(
        run_name="phase19-2d-deterministic",
        tags={
            "phase": "19.2d",
            "implementation": "direct_python_databricks",
            "environment": "dev",
            "evidence": "synthetic",
            "model_calls": "0",
            "llm_judges": "0",
            **provenance,
        },
    ) as active:
        results = mlflow.genai.evaluate(
            data=data,
            predict_fn=predict,
            scorers=ALL_SCORERS,
        )
        print(json.dumps({"run_id": active.info.run_id}, indent=2))

    return results


def run_judged(cases: tuple[dict[str, Any], ...]) -> int:
    """One judged evaluation over exactly three cases.

    A SEPARATE run in the SAME experiment. It does not touch the deterministic
    run: deterministic and judged results are different kinds of evidence and
    merging them into one run would let a judge verdict be mistaken for a gate.
    """
    global _TRACING

    from evaluation.mlflow_judge import (
        BUDGET,
        JUDGE_MODEL,
        JUDGE_NAME,
        JUDGED_CASES,
        evidence_discipline,
    )

    selected = tuple(c for c in cases if c["id"] in JUDGED_CASES)
    if len(selected) != len(JUDGED_CASES):
        raise SystemExit(f"expected {len(JUDGED_CASES)} judged cases, found {len(selected)}")

    _TRACING = resolve_config(experiment=EXPERIMENT)
    if os.environ.get(MAX_WORKERS_VAR) != "1":
        raise SystemExit(f"{MAX_WORKERS_VAR} must be 1 for the judged run")

    import mlflow

    with bridged_credentials(_TRACING):
        activate(_TRACING)
        data = build_data(selected)
        provenance = {**base_metadata(), **git_provenance()}

        with mlflow.start_run(
            run_name="phase19-2d-judged",
            tags={
                "phase": "19.2d",
                "implementation": "direct_python_databricks",
                "environment": "dev",
                "evidence": "synthetic",
                "evaluation_kind": "llm_judge",
                "judge_endpoint": JUDGE_MODEL,
                "judge_name": JUDGE_NAME,
                "max_endpoint_calls": str(BUDGET.limit),
                **provenance,
            },
        ) as active:
            results = mlflow.genai.evaluate(
                data=data,
                predict_fn=predict,
                scorers=[evidence_discipline],
            )
            print(
                json.dumps(
                    {
                        "run_id": active.info.run_id,
                        "endpoint_calls_used": BUDGET.used,
                        "metrics": dict(getattr(results, "metrics", {}) or {}),
                    },
                    indent=2,
                    default=str,
                )
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evaluation.mlflow_runner")
    parser.add_argument("mode", choices=["plan", "local", "managed", "judge-plan", "judge"])
    args = parser.parse_args(argv)

    cases = load_cases()

    if args.mode == "plan":
        print(json.dumps(plan(cases, "plan"), indent=2))
        return 0

    if args.mode == "judge-plan":
        from evaluation.mlflow_judge import dry_run

        print(json.dumps(dry_run(EXPERIMENT), indent=2))
        return 0

    if args.mode == "judge":
        return run_judged(cases)

    results = run_evaluation(args.mode, cases)
    metrics: Mapping[str, Any] = getattr(results, "metrics", {}) or {}
    print(json.dumps({"metrics": dict(metrics)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
