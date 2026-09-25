"""Offline fixtures for the G3 tests: fake price evidence, discovery, policy."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from g3lib import canonical_hash, read_json
from policy import BudgetPolicy, policy_from_dict

POLICY_PATH = Path(__file__).resolve().parents[1] / "deploy" / "g3" / "budget-policy.v1.json"

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
REGION = "uksouth"

# Retail prices observed for UK South on 2026-09-17 (USD). Unit prices only;
# the tests exercise arithmetic and selection, not the live API.
PRICES = {
    "vm_cpu_hour": ("0.187", "1 Hour"),
    "vm_gpu_hour": ("0.615", "1 Hour"),
    "os_disk_p6_month": ("12.3499", "1/Month"),
    "lb_rules_hour": ("0.025", "1 Hour"),
    "lb_data_gb": ("0.005", "1 GB"),
    "public_ip_hour": ("0.005", "1 Hour"),
    "egress_gb": ("0.087", "1 GB"),
    "acr_basic_day": ("0.1666", "1/Day"),
    "acr_storage_gb_month": ("0.1", "1 GB/Month"),
    "automation_minute": ("0.002", "1 Minute"),
    "blob_hot_gb_month": ("0.0192", "1 GB/Month"),
}


def policy() -> BudgetPolicy:
    return policy_from_dict(read_json(POLICY_PATH))


def evidence(
    now: datetime = NOW, currency: str = "USD", region: str = REGION, drop: str | None = None
) -> dict[str, Any]:
    entries = {
        key: {
            "key": key,
            "currency": currency,
            "arm_region": region,
            "unit_price": price,
            "unit": unit,
            "meter": key,
            "price_type": "Consumption",
        }
        for key, (price, unit) in PRICES.items()
        if key != drop
    }
    return {
        "schema_version": "1",
        "currency": currency,
        "region": region,
        "retrieved_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries": entries,
        "evidence_hash": canonical_hash({"entries": entries, "region": region}),
    }


def discovery(
    now: datetime = NOW, blockers: list[str] | None = None, roles: list[str] | None = None
) -> dict[str, Any]:
    blockers = blockers or []
    return {
        "schema_version": "1",
        "retrieved_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "subscription_id": SUBSCRIPTION,
        "region": REGION,
        "caller_roles_at_subscription": roles if roles is not None else ["Owner"],
        "evaluation": {
            "blockers": blockers,
            "warnings": [],
            "quota_verified": not any("quota" in b for b in blockers),
            "permissions_verified": "Owner" in (roles if roles is not None else ["Owner"]),
            "providers_verified": True,
        },
    }


def config_dict(gpu: bool = True) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "region": REGION,
        "system_node_vm_size": "Standard_D4als_v6",
        "system_node_count": 1,
        "gpu_node_pool_enabled": gpu,
        "gpu_node_vm_size": "Standard_NC4as_T4_v3",
        "gpu_node_count": 1,
        "os_disk_size_gb": 64,
        "kubernetes_version": "1.35.7",
    }
