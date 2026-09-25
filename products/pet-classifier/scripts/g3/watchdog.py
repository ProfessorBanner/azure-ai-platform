"""Reference model of the expiry watchdog and a local report-only runner.

The live watchdog is the Azure Automation runbook
``infrastructure/capabilities/aks-mlops/controls/runbooks/Invoke-SessionExpiry.ps1``.
This module implements the SAME eligibility rules and execution shape in
Python so they can be tested offline with a fake client, and so an operator
can run a report-only inventory from the laptop. Execution mode exists for
G4 but is disabled by default and is never invoked in G3.

Eligibility (all required):
  exact subscription, exact allowlisted resource-group id, group tags
  project / session_id / expiry_utc / lifecycle=disposable matching the armed
  values, schedule session id equal to the armed id, expiry reached, and each
  resource tagged lifecycle=disposable with the same session_id.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from g3lib import G3Error, format_utc, parse_utc, utc_now

EXECUTE_ENV = "G3_ALLOW_CLOUD_MUTATION"


@dataclass(frozen=True)
class Allowlist:
    subscription_id: str
    project: str
    session_rg_id: str
    node_rg_id: str
    armed_session_id: str
    armed_expiry_utc: str


@dataclass(frozen=True)
class Resource:
    id: str
    type: str
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResourceGroup:
    id: str
    tags: dict[str, str] = field(default_factory=dict)


class ArmClient(Protocol):
    def current_subscription(self) -> str: ...
    def get_resource_group(self, rg_id: str) -> ResourceGroup | None: ...
    def list_resources(self, rg_id: str) -> list[Resource]: ...
    def get_resource(self, resource_id: str) -> Resource | None: ...
    def delete_resource(self, resource_id: str) -> None: ...


def rg_name(rg_id: str) -> str:
    return rg_id.rsplit("/", 1)[-1]


def evaluate_eligibility(
    allow: Allowlist,
    client_subscription: str,
    group: ResourceGroup | None,
    now: datetime,
    schedule_session_id: str | None = None,
) -> list[str]:
    """Reasons the armed session is NOT eligible; empty means eligible."""
    reasons: list[str] = []
    if client_subscription != allow.subscription_id:
        reasons.append(f"subscription mismatch: {client_subscription} != {allow.subscription_id}")
    expected_prefix = f"/subscriptions/{allow.subscription_id}/resourceGroups/"
    if not allow.session_rg_id.startswith(expected_prefix):
        reasons.append("allowlisted group id is not in the configured subscription")
    if allow.armed_session_id in ("", "none"):
        reasons.append("no session armed")
    if schedule_session_id and schedule_session_id != allow.armed_session_id:
        reasons.append(
            f"schedule session id {schedule_session_id} != armed {allow.armed_session_id}"
        )
    if group is None:
        reasons.append("session group not found")
        return reasons
    if group.id != allow.session_rg_id:
        reasons.append(f"group id {group.id} is not the allowlisted {allow.session_rg_id}")
    tags = group.tags
    if tags.get("project") != allow.project:
        reasons.append(f"group project tag {tags.get('project')!r} != {allow.project!r}")
    if tags.get("lifecycle") != "disposable":
        reasons.append("group is not tagged lifecycle=disposable")
    if tags.get("session_id") != allow.armed_session_id:
        reasons.append(
            f"group session_id tag {tags.get('session_id')!r} != armed {allow.armed_session_id!r}"
        )
    if tags.get("expiry_utc") != allow.armed_expiry_utc:
        reasons.append("group expiry_utc tag differs from the armed expiry")
    try:
        expiry = parse_utc(allow.armed_expiry_utc)
    except G3Error:
        reasons.append(f"armed expiry {allow.armed_expiry_utc!r} is not RFC 3339 UTC")
        return reasons
    if now < expiry:
        reasons.append(f"not expired: now {format_utc(now)} < expiry {allow.armed_expiry_utc}")
    return reasons


def resource_eligible(resource: Resource, allow: Allowlist) -> bool:
    return (
        resource.tags.get("lifecycle") == "disposable"
        and resource.tags.get("session_id") == allow.armed_session_id
    )


def run(
    client: ArmClient,
    allow: Allowlist,
    mode: str,
    now_fn: Callable[[], datetime],
    schedule_session_id: str | None = None,
    sleep: Callable[[float], None] = lambda _: None,
    poll_interval_seconds: float = 30,
    max_poll_minutes: int = 40,
    max_attempts: int = 3,
) -> dict[str, Any]:
    if mode not in ("Report", "Execute"):
        raise G3Error("mode must be Report or Execute")
    started = now_fn()
    result: dict[str, Any] = {
        "schema_version": "1",
        "started_utc": format_utc(started),
        "mode": mode,
        "session_id": allow.armed_session_id,
        "eligible": False,
        "reasons": [],
        "inventory": [],
        "deleted": [],
        "unresolved": [],
        "node_rg_state": "unknown",
        "outcome": "not-eligible",
    }
    group = client.get_resource_group(allow.session_rg_id)
    reasons = evaluate_eligibility(
        allow, client.current_subscription(), group, started, schedule_session_id
    )
    result["reasons"] = reasons
    if group is None:
        result["outcome"] = "nothing-to-do"
        return _finish(result, now_fn)

    resources = client.list_resources(allow.session_rg_id)
    result["inventory"] = [
        {"id": r.id, "type": r.type, "eligible": resource_eligible(r, allow)} for r in resources
    ]
    node_group = client.get_resource_group(allow.node_rg_id)
    result["node_rg_state"] = "present" if node_group else "absent"
    result["eligible"] = not reasons
    if reasons:
        return _finish(result, now_fn)
    if not resources and node_group is None:
        result["outcome"] = "already-clean"
        return _finish(result, now_fn)
    if mode == "Report":
        result["outcome"] = "report-only"
        return _finish(result, now_fn)

    ordered = sorted(
        resources, key=lambda r: 0 if r.type == "Microsoft.ContainerService/managedClusters" else 1
    )
    for resource in ordered:
        if not resource_eligible(resource, allow):
            result["unresolved"].append(
                {
                    "id": resource.id,
                    "reason": "not this session's disposable resource; left in place",
                }
            )
            continue
        done = False
        for _attempt in range(max_attempts):
            if client.get_resource(resource.id) is None:
                done = True
                break
            try:
                client.delete_resource(resource.id)
            except Exception as exc:  # noqa: BLE001 - every failure is retried then reported
                result.setdefault("errors", []).append(f"{resource.id}: {exc}")
                continue
            deadline = now_fn() + timedelta(minutes=max_poll_minutes)
            while now_fn() < deadline:
                if client.get_resource(resource.id) is None:
                    done = True
                    break
                sleep(poll_interval_seconds)
            if done:
                break
        if done:
            result["deleted"].append(resource.id)
        else:
            result["unresolved"].append(
                {"id": resource.id, "reason": "deletion not confirmed within the job budget"}
            )

    remaining = client.list_resources(allow.session_rg_id)
    known = {u["id"] for u in result["unresolved"]}
    for resource in remaining:
        if resource.id not in known:
            result["unresolved"].append(
                {"id": resource.id, "reason": "still present after deletion pass"}
            )
    node_after = client.get_resource_group(allow.node_rg_id)
    result["node_rg_state"] = "present" if node_after else "absent"
    if node_after is not None:
        result["unresolved"].append(
            {"id": allow.node_rg_id, "reason": "AKS node resource group still present"}
        )
    result["outcome"] = "clean" if not result["unresolved"] else "unresolved"
    return _finish(result, now_fn)


def _finish(result: dict[str, Any], now_fn: Callable[[], datetime]) -> dict[str, Any]:
    result["finished_utc"] = format_utc(now_fn())
    return result


class AzCliClient:
    """Read-only unless ``allow_mutation`` is set; every call names the subscription."""

    def __init__(self, subscription_id: str, allow_mutation: bool = False) -> None:
        self.subscription_id = subscription_id
        self.allow_mutation = allow_mutation

    def _az(self, *args: str) -> Any:
        completed = subprocess.run(
            ["az", *args, "--subscription", self.subscription_id, "--output", "json"],
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            if "ResourceGroupNotFound" in completed.stderr or "NotFound" in completed.stderr:
                return None
            raise G3Error(completed.stderr.strip()[:300])
        return json.loads(completed.stdout or "null")

    def current_subscription(self) -> str:
        account = self._az("account", "show")
        return str(account["id"]) if account else ""

    def get_resource_group(self, rg_id: str) -> ResourceGroup | None:
        group = self._az("group", "show", "--name", rg_name(rg_id))
        if not group:
            return None
        return ResourceGroup(id=group["id"], tags=group.get("tags") or {})

    def list_resources(self, rg_id: str) -> list[Resource]:
        listed = self._az("resource", "list", "--resource-group", rg_name(rg_id)) or []
        return [Resource(id=r["id"], type=r["type"], tags=r.get("tags") or {}) for r in listed]

    def get_resource(self, resource_id: str) -> Resource | None:
        found = self._az("resource", "show", "--ids", resource_id)
        if not found:
            return None
        return Resource(id=found["id"], type=found["type"], tags=found.get("tags") or {})

    def delete_resource(self, resource_id: str) -> None:
        if not self.allow_mutation:
            raise G3Error("mutation disabled: this client is read-only")
        self._az("resource", "delete", "--ids", resource_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local watchdog run (report-only by default).")
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--project", default="pet-classifier-aks-mlops")
    parser.add_argument("--session-rg", default="rg-aiplatform-aksmlops-session")
    parser.add_argument("--node-rg", default="rg-aiplatform-aksmlops-session-nodes")
    parser.add_argument("--armed-session-id", required=True)
    parser.add_argument("--armed-expiry-utc", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help=f"FUTURE (G4): delete eligible resources; also needs {EXECUTE_ENV}=1. Not in G3.",
    )
    args = parser.parse_args(argv)
    execute = args.execute and os.environ.get(EXECUTE_ENV) == "1"
    if args.execute and not execute:
        raise G3Error(f"--execute refused: {EXECUTE_ENV} is not set to 1")
    allow = Allowlist(
        subscription_id=args.subscription,
        project=args.project,
        session_rg_id=f"/subscriptions/{args.subscription}/resourceGroups/{args.session_rg}",
        node_rg_id=f"/subscriptions/{args.subscription}/resourceGroups/{args.node_rg}",
        armed_session_id=args.armed_session_id,
        armed_expiry_utc=args.armed_expiry_utc,
    )
    client = AzCliClient(args.subscription, allow_mutation=execute)
    result = run(
        client, allow, "Execute" if execute else "Report", utc_now, sleep=__import__("time").sleep
    )
    print(json.dumps(result, indent=2))
    return (
        0
        if result["outcome"]
        in ("report-only", "already-clean", "clean", "nothing-to-do", "not-eligible")
        else 1
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except G3Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
