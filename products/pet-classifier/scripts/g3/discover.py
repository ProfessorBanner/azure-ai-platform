"""Read-only Azure preflight discovery.

Every command names the intended subscription explicitly; the CLI default is
never used. Nothing here mutates Azure: no provider registration, no quota
request, no role assignment. Findings are written to ``.local/g3/discovery``
(private: they contain the subscription and tenant ids) and summarised into
blockers that admission refuses to proceed past.

Quota is not capacity and a listed SKU is not quota: the three are recorded
separately and never inferred from one another.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from g3lib import (
    DISCOVERY_DIR,
    G3Error,
    atomic_write_json,
    format_utc,
    parse_utc,
    read_json,
    require_subscription,
    utc_now,
)

Runner = Callable[[Sequence[str]], Any]

REQUIRED_PROVIDERS = (
    "Microsoft.ContainerService",
    "Microsoft.Compute",
    "Microsoft.Network",
    "Microsoft.Automation",
    "Microsoft.ContainerRegistry",
    "Microsoft.Storage",
    "Microsoft.ManagedIdentity",
    "Microsoft.Authorization",
    "Microsoft.CostManagement",
)
PLANNED_RESOURCE_GROUPS = (
    "rg-aiplatform-aksmlops-controls",
    "rg-aiplatform-aksmlops-retained",
    "rg-aiplatform-aksmlops-session",
    "rg-aiplatform-aksmlops-session-nodes",
)
SUFFICIENT_ROLES = ("Owner",)


def az_runner(args: Sequence[str]) -> Any:
    """Run ``az`` and parse JSON; the caller already appended --subscription."""
    completed = subprocess.run(
        ["az", *args, "--output", "json"], check=False, text=True, capture_output=True
    )
    if completed.returncode != 0:
        raise G3Error(f"az {' '.join(args[:3])} failed: {completed.stderr.strip()[:300]}")
    return json.loads(completed.stdout or "null")


def sku_capabilities(sku: dict[str, Any]) -> dict[str, str]:
    return {c["name"]: str(c["value"]) for c in sku.get("capabilities", [])}


def discover(
    subscription_id: str,
    region: str,
    cpu_sku: str,
    gpu_sku: str,
    run: Runner = az_runner,
    now: datetime | None = None,
) -> dict[str, Any]:
    sub = ["--subscription", subscription_id]
    account = run(["account", "show", *sub])
    if account["id"] != subscription_id:
        raise G3Error("az account show returned a different subscription than requested")

    providers = {
        name: run(["provider", "show", "--namespace", name, "--query", "registrationState", *sub])
        for name in REQUIRED_PROVIDERS
    }

    skus: dict[str, Any] = {}
    for sku_name in (cpu_sku, gpu_sku):
        listed = run(["vm", "list-skus", "--location", region, "--size", sku_name, *sub])
        exact = [s for s in listed if s["name"] == sku_name]
        if not exact:
            skus[sku_name] = {
                "listed": False,
                "restrictions": [],
                "family": None,
                "capabilities": {},
            }
            continue
        sku = exact[0]
        skus[sku_name] = {
            "listed": True,
            "family": sku.get("family"),
            "restrictions": [
                {"type": r.get("type"), "reason": r.get("reasonCode")}
                for r in sku.get("restrictions", [])
            ],
            "capabilities": {
                k: v
                for k, v in sku_capabilities(sku).items()
                if k in ("vCPUs", "MemoryGB", "GPUs", "CpuArchitectureType")
            },
        }

    usage = run(["vm", "list-usage", "--location", region, *sub])
    quota = {
        u["name"]["value"]: {"current": int(u["currentValue"]), "limit": int(u["limit"])}
        for u in usage
    }

    versions = run(["aks", "get-versions", "--location", region, *sub])
    aks_versions = [
        {
            "minor": v["version"],
            "default": bool(v.get("isDefault")),
            "preview": bool(v.get("isPreview")),
            "patches": sorted((v.get("patchVersions") or {}).keys()),
        }
        for v in versions.get("values", [])
    ]

    signed_in = run(["ad", "signed-in-user", "show", "--query", "id"])
    assignments = run(
        [
            "role",
            "assignment",
            "list",
            "--assignee",
            signed_in,
            "--scope",
            f"/subscriptions/{subscription_id}",
            "--include-inherited",
            "--query",
            "[].roleDefinitionName",
            *sub,
        ]
    )

    groups = run(["group", "list", "--query", "[].name", *sub])
    existing = sorted(set(groups) & set(PLANNED_RESOURCE_GROUPS))

    try:
        cost_probe = run(
            [
                "rest",
                "--method",
                "post",
                "--url",
                f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query?api-version=2023-11-01",
                "--body",
                '{"type":"ActualCost","timeframe":"MonthToDate","dataset":{"granularity":"None","aggregation":{"totalCost":{"name":"Cost","function":"Sum"}}}}',
            ]
        )
        cost_access = {
            "available": True,
            "columns": [c["name"] for c in cost_probe["properties"]["columns"]],
        }
    except G3Error as exc:
        cost_access = {"available": False, "error": str(exc)[:200]}

    raw = {
        "schema_version": "1",
        "retrieved_utc": format_utc(now or utc_now()),
        "subscription_id": subscription_id,
        "tenant_id": account["tenantId"],
        "region": region,
        "providers": providers,
        "skus": skus,
        "quota": quota,
        "aks_versions": aks_versions,
        "caller_roles_at_subscription": sorted(set(assignments)),
        "existing_planned_resource_groups": existing,
        "cost_management": cost_access,
        "cpu_sku": cpu_sku,
        "gpu_sku": gpu_sku,
    }
    raw["evaluation"] = evaluate(raw)
    return raw


def evaluate(raw: dict[str, Any]) -> dict[str, Any]:
    """Blockers and verified flags derived from the raw findings. Pure."""
    blockers: list[str] = []
    warnings: list[str] = []
    for name, state in raw["providers"].items():
        if state != "Registered":
            blockers.append(
                f"provider {name} is {state}; registration is a G4 human action, not done here"
            )

    cpu, gpu = raw["skus"][raw["cpu_sku"]], raw["skus"][raw["gpu_sku"]]
    for label, sku, name in (("system", cpu, raw["cpu_sku"]), ("gpu", gpu, raw["gpu_sku"])):
        if not sku["listed"]:
            blockers.append(f"{label} SKU {name} is not listed in {raw['region']}")
        elif sku["restrictions"]:
            blockers.append(f"{label} SKU {name} is restricted: {sku['restrictions']}")
        elif sku["capabilities"].get("CpuArchitectureType") != "x64":
            blockers.append(f"{label} SKU {name} is not x86-64")

    quota = raw["quota"]
    needed = {}
    for label, sku, _name in (("system", cpu, raw["cpu_sku"]), ("gpu", gpu, raw["gpu_sku"])):
        family = sku.get("family")
        vcpus = int(sku["capabilities"].get("vCPUs", "0")) if sku["listed"] else 0
        needed[label] = {"family": family, "vcpus": vcpus}
        if family is None:
            continue
        entry = quota.get(family)
        if entry is None:
            blockers.append(f"{label} family {family} has no quota entry in {raw['region']}")
        elif entry["limit"] - entry["current"] < vcpus:
            blockers.append(
                f"{label} family {family} quota {entry['current']}/{entry['limit']} vCPU "
                f"cannot fit {vcpus} vCPU; a quota request is a G4 human action, not done here"
            )
    total = quota.get("cores")
    total_needed = needed["system"]["vcpus"] + needed["gpu"]["vcpus"]
    if total is None or total["limit"] - total["current"] < total_needed:
        blockers.append(f"regional total vCPU quota {total} cannot fit {total_needed}")

    supported = [v for v in raw["aks_versions"] if not v["preview"]]
    if not supported:
        blockers.append("no supported non-preview AKS version in region")

    roles = set(raw["caller_roles_at_subscription"])
    permissions_verified = bool(roles & set(SUFFICIENT_ROLES))
    if not permissions_verified:
        blockers.append(
            "caller permissions unresolved: no subscription-level Owner; "
            f"roles seen: {sorted(roles) or 'none'} "
            "(Contributor plus role-assignment rights would also do)"
        )
    if "Owner" in roles:
        warnings.append(
            "caller is subscription Owner: broader than the watchdog identity ever gets"
        )

    if raw["existing_planned_resource_groups"]:
        warnings.append(
            f"planned resource groups already exist: {raw['existing_planned_resource_groups']} "
            "(fine after the controls apply; a collision before it)"
        )
    if not raw["cost_management"]["available"]:
        warnings.append(
            "Cost Management query unavailable now; "
            "reconciliation must wait or use the portal export"
        )

    return {
        "blockers": blockers,
        "warnings": warnings,
        "quota_verified": not any("quota" in b for b in blockers),
        "permissions_verified": permissions_verified,
        "providers_verified": not any("provider" in b for b in blockers),
        "needed_vcpus": needed,
        "gpu_capacity_note": (
            "Quota and SKU listing do not prove capacity; "
            "only a successful node pool creation does."
        ),
    }


def check_discovery(
    discovery: dict[str, Any], now: datetime, max_age_hours: int, subscription_id: str, region: str
) -> list[str]:
    reasons: list[str] = []
    if discovery.get("subscription_id") != subscription_id:
        reasons.append("discovery is for a different subscription")
    if discovery.get("region") != region:
        reasons.append("discovery is for a different region")
    retrieved = discovery.get("retrieved_utc")
    if not isinstance(retrieved, str):
        reasons.append("discovery has no retrieval timestamp")
    elif now - parse_utc(retrieved) > timedelta(hours=max_age_hours):
        reasons.append(f"discovery older than {max_age_hours} h")
    return reasons


def load_latest_discovery() -> dict[str, Any]:
    path = DISCOVERY_DIR / "latest.json"
    if not path.is_file():
        raise G3Error(f"no discovery at {path}; run scripts/g3/discover.py first")
    return read_json(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Azure preflight discovery.")
    parser.add_argument("--region", default="uksouth")
    parser.add_argument("--cpu-sku", default="Standard_D4als_v6")
    parser.add_argument("--gpu-sku", default="Standard_NC4as_T4_v3")
    args = parser.parse_args(argv)

    subscription_id = require_subscription()
    findings = discover(subscription_id, args.region, args.cpu_sku, args.gpu_sku)
    stamp = findings["retrieved_utc"].replace(":", "").replace("-", "")
    path = DISCOVERY_DIR / f"{stamp}-{args.region}.json"
    atomic_write_json(path, findings)
    atomic_write_json(DISCOVERY_DIR / "latest.json", findings)
    evaluation = findings["evaluation"]
    print(f"discovery written to {path}")
    print(f"blockers: {len(evaluation['blockers'])}")
    for blocker in evaluation["blockers"]:
        print(f"  BLOCKER {blocker}")
    for warning in evaluation["warnings"]:
        print(f"  warning {warning}")
    return 0 if not evaluation["blockers"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except G3Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
