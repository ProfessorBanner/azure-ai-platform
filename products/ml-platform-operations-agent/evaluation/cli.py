"""Offline evaluation runner.

    uv run python -m evaluation.cli run
    uv run python -m evaluation.cli run --json

Runs every case in `cases.jsonl` against the deterministic fixtures, applies the
scorers, and exits non-zero if any gate fails. NO NETWORK, NO LLM, NO CLOCK —
the fixtures are anchored to a constant `NOW`, so two runs of this command
produce byte-identical results and the determinism gate is meaningful.

THE DETERMINISM GATE IS RUN, NOT ASSUMED
-----------------------------------------
Each case is evaluated TWICE and the serialised diagnoses compared. Asserting
determinism by inspecting the code would miss exactly the failure it is meant to
catch — a set iterated without sorting, a dict ordering that happens to be
stable today — because those are properties of a run, not of a reading.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from evaluation.scorers import GATES, score_all
from ml_platform_operations_agent.adapters.fake import SCENARIOS, Scenario, window
from ml_platform_operations_agent.agent import (
    DiagnosisRequest,
    EvidenceSources,
    OperationsAgent,
)
from ml_platform_operations_agent.domain import Diagnosis

CASES_PATH = Path(__file__).parent / "cases.jsonl"


def load_cases(path: Path = CASES_PATH) -> tuple[dict[str, Any], ...]:
    """Read the JSONL case file, skipping blank lines and `#` comments."""
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cases.append(json.loads(stripped))
    return tuple(cases)


def run_case(case: Mapping[str, Any]) -> Diagnosis:
    """Build the agent for one scenario and answer its question."""
    scenario: Scenario = SCENARIOS[case["scenario"]]
    sources = EvidenceSources(
        registry_source=scenario.registry,
        run_history=scenario.run_history,
        monitoring=scenario.monitoring,
        runbooks=scenario.runbooks,
    )
    agent = OperationsAgent(sources)
    days = int(case.get("window_days", 7))
    from ml_platform_operations_agent.adapters.fake import NOW

    return agent.run(
        DiagnosisRequest(
            model_name=case.get("model_name") or scenario.model_name,
            window=window(days),
            observed_at=NOW,
            question=case.get("question") or scenario.question,
        )
    )


def evaluate(cases: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Score every case and compute the gate table."""
    totals: dict[str, list[bool]] = {}
    failures: list[dict[str, str]] = []
    non_deterministic: list[str] = []

    for case in cases:
        first = run_case(case)
        second = run_case(case)
        if first.model_dump_json() != second.model_dump_json():
            non_deterministic.append(case["id"])

        for result in score_all(first, case.get("expected", {})):
            totals.setdefault(result.name, []).append(result.passed)
            if not result.passed:
                failures.append(
                    {"case": case["id"], "scorer": result.name, "detail": result.detail}
                )

    rates = {name: sum(values) / len(values) for name, values in sorted(totals.items())}
    rates["determinism"] = 1.0 - (len(non_deterministic) / len(cases) if cases else 0.0)

    gate_rows = [
        {
            "gate": name,
            "required": GATES[name],
            "actual": round(rates.get(name, 0.0), 4),
            "passed": rates.get(name, 0.0) >= GATES[name] - 1e-9,
        }
        for name in sorted(GATES)
        if name in rates
    ]

    return {
        "case_count": len(cases),
        "gates": gate_rows,
        "failures": failures,
        "non_deterministic_cases": non_deterministic,
        "all_gates_passed": all(row["passed"] for row in gate_rows),
    }


def _render(report: Mapping[str, Any]) -> Iterator[str]:
    yield f"cases: {report['case_count']}"
    yield ""
    yield f"{'gate':<34} {'required':>9} {'actual':>8}  result"
    yield "-" * 62
    for row in report["gates"]:
        mark = "PASS" if row["passed"] else "FAIL"
        yield f"{row['gate']:<34} {row['required']:>9.2f} {row['actual']:>8.2f}  {mark}"
    if report["failures"]:
        yield ""
        yield "failures:"
        for failure in report["failures"]:
            yield f"  {failure['case']:<28} {failure['scorer']}: {failure['detail']}"
    if report["non_deterministic_cases"]:
        yield ""
        yield "NON-DETERMINISTIC: " + ", ".join(report["non_deterministic_cases"])
    yield ""
    yield (
        "OK - all gates passed." if report["all_gates_passed"] else "FAILED - a gate did not pass."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evaluation.cli", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run the offline evaluation suite.")
    run.add_argument("--json", action="store_true", help="Emit the raw report as JSON.")

    args = parser.parse_args(argv)
    report = evaluate(load_cases())

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for line in _render(report):
            print(line)

    return 0 if report["all_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
