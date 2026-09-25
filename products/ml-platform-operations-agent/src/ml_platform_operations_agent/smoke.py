"""The live read-only proof. The ONLY module that constructs SDK clients.

    DATABRICKS_CONFIG_PROFILE=aiplatform-dev \\
      uv run --group live python -m ml_platform_operations_agent.smoke \\
        --model dev.ml_lifecycle_demo.linear_regression_model \\
        --window-days 7 --read-only

WHY THE COMPOSITION ROOT IS SEPARATE
------------------------------------
`adapters/databricks.py` takes its clients as parameters and imports neither
`mlflow` nor `databricks.sdk`. This module is where those imports live, and it
is the only file in `src/` exempted from the forbidden-import hygiene test.
That keeps the exemption to one reviewable file instead of a package-wide
loosening, and it means every adapter stays testable without credentials.

`--read-only` IS MANDATORY AND MEANS SOMETHING
-----------------------------------------------
It is not a courtesy flag. There is no code path in this package that writes
anything, so the flag documents the guarantee and forces the operator running
it to state the intent. Omitting it is an error rather than a different mode,
because a "different mode" would imply a writing one exists.

WHAT THIS WILL NOT DO
---------------------
Start the SQL warehouse. Run a job. Move an alias. Create an MLflow experiment,
run or trace. Invoke a model endpoint. Change a permission. The absence of that
capability is asserted by `tests/test_live_adapters.py`, not promised here.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ml_platform_operations_agent.adapters.databricks import (
    DatabricksModelRegistry,
    DatabricksMonitoring,
    DatabricksRunHistory,
    RepositoryRunbooks,
    require_profile,
)
from ml_platform_operations_agent.agent import (
    DiagnosisRequest,
    EvidenceSources,
    OperationsAgent,
)
from ml_platform_operations_agent.config import parse_model_name, scope_for
from ml_platform_operations_agent.domain import Diagnosis, TimeWindow
from ml_platform_operations_agent.errors import ConfigurationError

#: Walk up to the repository root. The runbooks are repository-resident
#: (19.2a: no UC volume exists), so the agent needs to know where the
#: repository is — derived, not configured, so it cannot be pointed elsewhere.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def build_live_sources(profile: str, catalog: str, window: TimeWindow) -> EvidenceSources:
    """Construct the four live adapters. The only place SDK clients are made."""
    from databricks.sdk import WorkspaceClient
    from mlflow.tracking import MlflowClient

    workspace = WorkspaceClient(profile=profile)

    # Registry URI `databricks-uc`, not `databricks`: the models live in Unity
    # Catalog, and the legacy workspace registry would resolve a different
    # namespace entirely. Tracking URI is set for the run reads.
    mlflow_client = MlflowClient(tracking_uri="databricks", registry_uri="databricks-uc")

    scope = scope_for(catalog)
    registry = DatabricksModelRegistry(client=workspace, scope=scope, window=window)

    return EvidenceSources(
        registry_source=registry,
        run_history=DatabricksRunHistory(mlflow_client=mlflow_client, registry=registry),
        monitoring=DatabricksMonitoring(client=workspace, scope=scope),
        runbooks=RepositoryRunbooks(repository_root=REPOSITORY_ROOT, window=window),
    )


def _summarise(diagnosis: Diagnosis) -> dict[str, Any]:
    """A sanitised record of one proof.

    Identifiers, enums and counts. No workspace URL, no principal, no token, no
    raw payload — everything here is safe to paste into a document.
    """
    return {
        "requested_model": diagnosis.requested_model,
        "resolved_model": diagnosis.resolved_model,
        "resolved_version": diagnosis.resolved_version,
        "degradation_status": diagnosis.degradation_status.value,
        "outcome": diagnosis.outcome.value,
        "refusal_reason": diagnosis.refusal_reason.value if diagnosis.refusal_reason else None,
        "confidence": diagnosis.confidence,
        "selected_tools": list(diagnosis.selected_tools),
        "supported_cause_count": len(diagnosis.likely_causes),
        "evidence": [
            {"source_type": r.source_type.value, "source_identifier": r.source_identifier}
            for r in diagnosis.supporting_evidence
        ],
        "limitations": list(diagnosis.limitations),
        "recommended_investigations": list(diagnosis.recommended_investigations),
        "mutations": 0,
        "warehouse_started": False,
        "jobs_run": 0,
        "llm_calls": 0,
        "mlflow_writes": 0,
    }


def _provenance(sources: EvidenceSources, model: str) -> dict[str, Any]:
    """Proof A: current registry provenance, with alias casing made explicit."""
    from ml_platform_operations_agent.domain import EvidenceAbsence

    resolved = sources.registry_source.resolve_model(model)
    if isinstance(resolved, EvidenceAbsence):
        return {"resolved": False, "reason": resolved.reason}

    champion = resolved.alias("Champion")
    return {
        "resolved": True,
        "full_name": resolved.full_name,
        "aliases": [
            {"as_returned": b.name, "normalised": b.name.casefold(), "version": b.version}
            for b in resolved.aliases
        ],
        # Looked up with the Phase 15 casing `Champion`; Unity Catalog stores
        # `champion`. That this resolves at all is the case-insensitivity proof.
        "champion_lookup": {
            "queried_as": "Champion",
            "matched": champion is not None,
            "as_returned": champion.name if champion else None,
            "version": champion.version if champion else None,
        },
        "historical_alias_claim": None,
        "historical_alias_note": (
            "Unity Catalog exposes current alias state only; which version held "
            "an alias earlier is not determinable from this source."
        ),
        "versions": [
            {
                "version": v.version,
                "created_at": v.created_at.isoformat(),
                "run_id": v.run_id,
                "tags_available": v.tags_available,
                "tags": dict(sorted(v.tags.items())),
                "metrics": dict(sorted(v.metrics.items())),
            }
            for v in resolved.versions
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ml_platform_operations_agent.smoke")
    parser.add_argument("--model", required=True)
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument(
        "--read-only",
        action="store_true",
        required=True,
        help="Mandatory. This package has no writing mode; the flag states the intent.",
    )
    parser.add_argument("--proof", choices=["a", "b", "c", "d", "all"], default="all")
    args = parser.parse_args(argv)

    try:
        profile = require_profile()
    except ConfigurationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 3

    parsed = parse_model_name(args.model)
    if parsed is None:
        print("The model identifier is not a governed three-part name.", file=sys.stderr)
        return 3

    # `observed_at` is read ONCE here, at the composition root, and threaded
    # through everything. The agent and adapters never read a clock themselves.
    observed_at = datetime.now(UTC)
    window = TimeWindow(start=observed_at - timedelta(days=args.window_days), end=observed_at)

    sources = build_live_sources(profile, parsed[0], window)
    agent = OperationsAgent(sources)

    def ask(question: str, model: str = args.model) -> dict[str, Any]:
        return _summarise(
            agent.run(
                DiagnosisRequest(
                    model_name=model,
                    window=window,
                    observed_at=observed_at,
                    question=question,
                )
            )
        )

    report: dict[str, Any] = {
        "observed_at_utc": observed_at.isoformat(),
        "profile": profile,
        "window": window.label(),
        "read_only": True,
    }

    if args.proof in {"a", "all"}:
        report["proof_a_provenance"] = _provenance(sources, args.model)
    if args.proof in {"b", "all"}:
        report["proof_b_degradation"] = ask(
            f"Why did {args.model} degrade during the last {args.window_days} days?"
        )
    if args.proof in {"c", "all"}:
        report["proof_c_prohibited"] = ask("Retrain the model and make the new version Champion.")
    if args.proof in {"d", "all"}:
        unknown = f"{parsed[0]}.{parsed[1]}.not_a_registered_model"
        report["proof_d_unknown_model"] = ask(
            f"Why did {unknown} degrade during the last seven days?", unknown
        )

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
