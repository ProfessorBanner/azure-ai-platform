"""Operator CLI for the G3 cost controls.

Subcommands (all local except where noted; none mutates Azure):
  estimate      price one session configuration against the latest evidence
  admit         dry-run admission: "cost fits" and "cloud execution" separately
  reserve       write a reservation to the ledger (only when admission passes)
  status        lifetime ledger exposure
  resources     record resource ids created for a session (from Terraform output)
  teardown      record a watchdog result JSON as teardown evidence
  reconcile     record actual charges for a coverage window (from Cost Management)
  watchdog-verified
                record the G4 live verification of the watchdog (never in G3)
  bind          bind config + evidence + estimate + policy + plan hashes together
  verify-binding
                check a plan file still matches the reviewed binding
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import admission
import ledger as ledger_mod
from discover import load_latest_discovery
from estimate import config_from_dict, estimate, format_estimate
from g3lib import (
    CONTROLS_STATE_FILE,
    SESSIONS_DIR,
    G3Error,
    atomic_write_json,
    canonical_hash,
    format_utc,
    json_dumps,
    money,
    new_session_id,
    parse_utc,
    read_json,
    require_subscription,
    sha256_file,
    utc_now,
    validate_session_id,
)
from policy import load_policy
from prices import load_latest_evidence

SESSION_RESOURCE_GROUPS = ["rg-aiplatform-aksmlops-session", "rg-aiplatform-aksmlops-session-nodes"]


def load_watchdog_verification() -> admission.WatchdogVerification:
    if not CONTROLS_STATE_FILE.is_file():
        return admission.WatchdogVerification(live_verified=False)
    state = read_json(CONTROLS_STATE_FILE).get("watchdog", {})
    return admission.WatchdogVerification(
        live_verified=bool(state.get("live_verified", False)),
        verified_utc=state.get("verified_utc"),
        mode_verified=state.get("mode_verified"),
        evidence_ref=state.get("evidence_ref"),
    )


def _store(args: argparse.Namespace) -> ledger_mod.LedgerStore:
    if args.ledger:
        path = Path(args.ledger)
        return ledger_mod.LedgerStore(path, path.with_suffix(".lock"))
    return ledger_mod.LedgerStore()


def _admission(
    args: argparse.Namespace, bound_config_hash: str | None = None
) -> tuple[Any, Any, Any]:
    policy = load_policy()
    config = config_from_dict(read_json(Path(args.config)))
    evidence = load_latest_evidence()
    discovery = load_latest_discovery()
    est = estimate(config, policy, evidence)
    store = _store(args)
    exposure = ledger_mod.exposure(store.load(policy.policy_version))
    decision = admission.decide(
        policy,
        exposure,
        est,
        evidence,
        discovery,
        load_watchdog_verification(),
        utc_now(),
        require_subscription(),
        config.region,
        bound_config_hash,
    )
    return policy, est, decision


def print_decision(
    decision: admission.AdmissionDecision, exposure: ledger_mod.Exposure, limit: Any
) -> None:
    print(
        f"settled {exposure.settled_usd} + unresolved {exposure.unresolved_usd} "
        f"+ retained {exposure.retained_reserve_usd} USD committed"
    )
    print(
        f"projected total {decision.projected_total_usd} USD of {limit} USD admission limit "
        f"(headroom {decision.headroom_usd})"
    )
    print(f"cost fits: {'YES' if decision.cost_fits else 'NO'}")
    for reason in decision.cost_reasons:
        print(f"  - {reason}")
    print(f"cloud execution: {'ALLOWED' if decision.cloud_execution_allowed else 'BLOCKED'}")
    for reason in decision.control_reasons:
        print(f"  - {reason}")


def cmd_estimate(args: argparse.Namespace) -> int:
    policy = load_policy()
    config = config_from_dict(read_json(Path(args.config)))
    est = estimate(config, policy, load_latest_evidence())
    print(format_estimate(est))
    print(
        f"config {est.config_hash[:12]} evidence {est.evidence_hash[:12]} "
        f"policy {est.policy_hash[:12]}"
    )
    return 0


def cmd_admit(args: argparse.Namespace) -> int:
    policy, _est, decision = _admission(args)
    exposure = ledger_mod.exposure(_store(args).load(policy.policy_version))
    print_decision(decision, exposure, policy.admission_limit_usd)
    return 0 if decision.cloud_execution_allowed else 1


def cmd_reserve(args: argparse.Namespace) -> int:
    policy, est, decision = _admission(args)
    store = _store(args)
    if not decision.cloud_execution_allowed:
        exposure = ledger_mod.exposure(store.load(policy.policy_version))
        print_decision(decision, exposure, policy.admission_limit_usd)
        raise G3Error("reservation refused: admission did not pass (there is no bypass)")
    now = utc_now()
    expiry = now + timedelta(minutes=policy.expiry_minutes_after_reservation)
    session_id = args.session_id or new_session_id(now)
    with store.locked():
        book = store.load(policy.policy_version)
        book = ledger_mod.reserve(
            book,
            session_id,
            now,
            expiry,
            est.reservation_usd,
            est.config_hash,
            est.evidence_hash,
            est.policy_hash,
            SESSION_RESOURCE_GROUPS,
        )
        store.save(book)
    session_dir = SESSIONS_DIR / session_id
    atomic_write_json(session_dir / "estimate.json", est.as_dict())
    atomic_write_json(session_dir / "decision.json", decision.as_dict())
    when = format_utc(expiry)
    print(f"reserved {session_id}: {est.reservation_usd} USD, expiry {when} (from reservation)")
    print(f'arm with: armed_session = {{ session_id = "{session_id}", expiry_utc = "{when}" }}')
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    policy = load_policy()
    store = _store(args)
    book = store.load(policy.policy_version)
    exposure = ledger_mod.exposure(book)
    print(
        f"policy v{policy.policy_version}: project {policy.project_limit_usd}, "
        f"admission {policy.admission_limit_usd}, contingency {policy.contingency_usd} USD"
    )
    print(
        f"settled {exposure.settled_usd}  unresolved {exposure.unresolved_usd}  "
        f"retained {exposure.retained_reserve_usd}  committed {exposure.committed_usd} USD"
    )
    print(
        f"headroom under admission limit: {policy.admission_limit_usd - exposure.committed_usd} USD"
    )
    for session_id, record in sorted(book["sessions"].items()):
        amount, settled = ledger_mod.session_exposure(record)
        settled_label = "settled" if settled else "UNRESOLVED"
        teardown_label = "verified" if record["teardown"]["verified"] else "UNVERIFIED"
        print(
            f"  {session_id} {record['status']:12s} reserved {record['reserved_usd']:>8} "
            f"actual {record['actual_usd']:>8} counted {amount:>8} {settled_label} "
            f"teardown {teardown_label}"
        )
    print(f"live sessions: {exposure.live_sessions or 'none'}")
    print(f"watchdog live-verified: {load_watchdog_verification().live_verified}")
    return 0


def cmd_resources(args: argparse.Namespace) -> int:
    policy = load_policy()
    store = _store(args)
    with store.locked():
        book = store.load(policy.policy_version)
        book = ledger_mod.record_resources(
            book, validate_session_id(args.session_id), args.resource_id
        )
        store.save(book)
    print(f"recorded {len(args.resource_id)} resource id(s) for {args.session_id}")
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    policy = load_policy()
    result_path = Path(args.result)
    result = read_json(result_path)
    if result.get("session_id") != args.session_id:
        raise G3Error("watchdog result is for a different session")
    residuals = [u["id"] for u in result.get("unresolved", [])]
    if result.get("outcome") not in ("clean", "already-clean") and not residuals:
        residuals = [f"outcome:{result.get('outcome')}"]
    store = _store(args)
    with store.locked():
        book = store.load(policy.policy_version)
        book = ledger_mod.record_teardown(
            book, args.session_id, utc_now(), residuals, sha256_file(result_path)
        )
        store.save(book)
    print("teardown verified" if not residuals else f"teardown UNVERIFIED; residuals: {residuals}")
    return 0 if not residuals else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    policy = load_policy()
    store = _store(args)
    with store.locked():
        book = store.load(policy.policy_version)
        book = ledger_mod.reconcile(
            book,
            args.session_id,
            utc_now(),
            money(args.actual_usd),
            parse_utc(args.coverage_from),
            parse_utc(args.coverage_to),
            args.complete,
            args.source,
        )
        store.save(book)
    record = book["sessions"][args.session_id]
    amount, settled = ledger_mod.session_exposure(record)
    state = "settled" if settled else "still unresolved (reservation retained)"
    print(f"{args.session_id}: actual {record['actual_usd']} USD, counted {amount} USD, {state}")
    return 0


def cmd_watchdog_verified(args: argparse.Namespace) -> int:
    result_path = Path(args.result)
    result = read_json(result_path)
    if result.get("outcome") not in ("report-only", "already-clean", "clean"):
        raise G3Error(
            f"result outcome {result.get('outcome')!r} does not prove the watchdog completed"
        )
    if not args.job_id:
        raise G3Error("an Azure Automation job id is required as the live evidence reference")
    state = read_json(CONTROLS_STATE_FILE) if CONTROLS_STATE_FILE.is_file() else {}
    state["watchdog"] = {
        "live_verified": True,
        "verified_utc": format_utc(utc_now()),
        "mode_verified": result.get("mode"),
        "evidence_ref": f"automation-job:{args.job_id} result-sha256:{sha256_file(result_path)}",
    }
    atomic_write_json(CONTROLS_STATE_FILE, state)
    print("watchdog recorded as live-verified (G4 evidence)")
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    policy = load_policy()
    session_id = validate_session_id(args.session_id)
    session_dir = SESSIONS_DIR / session_id
    est = read_json(session_dir / "estimate.json")
    plan_path = Path(args.plan_json)
    plan = read_json(plan_path)
    binding = {
        "schema_version": "1",
        "session_id": session_id,
        "config_hash": est["config_hash"],
        "evidence_hash": est["evidence_hash"],
        "policy_hash": policy.policy_hash,
        "estimate_hash": canonical_hash(est),
        "reservation_usd": est["reservation_usd"],
        "plan_json_sha256": sha256_file(plan_path),
        "plan_resource_changes": sorted(
            f"{c['type']}.{c['name']}:{'/'.join(c['change']['actions'])}"
            for c in plan.get("resource_changes", [])
        ),
        "bound_utc": format_utc(utc_now()),
    }
    if any(":delete" in c or "/delete" in c for c in binding["plan_resource_changes"]):
        raise G3Error("plan contains deletions; a session plan may only add")
    atomic_write_json(session_dir / "binding.json", binding)
    print(json_dumps(binding))
    return 0


def cmd_verify_binding(args: argparse.Namespace) -> int:
    session_id = validate_session_id(args.session_id)
    binding = read_json(SESSIONS_DIR / session_id / "binding.json")
    problems: list[str] = []
    if sha256_file(Path(args.plan_json)) != binding["plan_json_sha256"]:
        problems.append("plan JSON differs from the bound plan")
    if load_policy().policy_hash != binding["policy_hash"]:
        problems.append("policy changed since binding")
    if load_latest_evidence().get("evidence_hash") != binding["evidence_hash"]:
        problems.append("price evidence changed since binding")
    est = read_json(SESSIONS_DIR / session_id / "estimate.json")
    if canonical_hash(est) != binding["estimate_hash"]:
        problems.append("estimate changed since binding")
    for problem in problems:
        print(f"BINDING MISMATCH: {problem}")
    print("binding verified" if not problems else "binding NOT verified")
    return 0 if not problems else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G3 cost controls operator CLI (no Azure mutation)."
    )
    parser.add_argument("--ledger", help="override the ledger path (tests and demonstrations)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("estimate")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_estimate)
    p = sub.add_parser("admit")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_admit)
    p = sub.add_parser("reserve")
    p.add_argument("--config", required=True)
    p.add_argument("--session-id")
    p.set_defaults(func=cmd_reserve)
    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("resources")
    p.add_argument("--session-id", required=True)
    p.add_argument("resource_id", nargs="+")
    p.set_defaults(func=cmd_resources)
    p = sub.add_parser("teardown")
    p.add_argument("--session-id", required=True)
    p.add_argument("--result", required=True)
    p.set_defaults(func=cmd_teardown)
    p = sub.add_parser("reconcile")
    p.add_argument("--session-id", required=True)
    p.add_argument("--actual-usd", required=True)
    p.add_argument("--coverage-from", required=True)
    p.add_argument("--coverage-to", required=True)
    p.add_argument("--complete", action="store_true")
    p.add_argument("--source", required=True)
    p.set_defaults(func=cmd_reconcile)
    p = sub.add_parser("watchdog-verified")
    p.add_argument("--result", required=True)
    p.add_argument("--job-id", required=True)
    p.set_defaults(func=cmd_watchdog_verified)
    p = sub.add_parser("bind")
    p.add_argument("--session-id", required=True)
    p.add_argument("--plan-json", required=True)
    p.set_defaults(func=cmd_bind)
    p = sub.add_parser("verify-binding")
    p.add_argument("--session-id", required=True)
    p.add_argument("--plan-json", required=True)
    p.set_defaults(func=cmd_verify_binding)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except G3Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
