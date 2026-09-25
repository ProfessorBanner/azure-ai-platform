"""Pure session cost estimation with Decimal arithmetic.

Input: a session configuration, the budget policy and price evidence.
Output: line items, allowances and a reservation total rounded UP to the
cent, plus the hashes that bind the three inputs together. No I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_UP, Decimal
from typing import Any

from g3lib import ZERO, G3Error, canonical_hash, round_up_cents
from policy import BudgetPolicy
from prices import unit_price

HOURS_PER_MONTH = Decimal("730")
MICRO = Decimal("0.000001")
ALLOWED_SYSTEM_SKUS = ("Standard_D4als_v6", "Standard_D4ls_v6", "Standard_D4as_v6")
ALLOWED_GPU_SKUS = ("Standard_NC4as_T4_v3",)


@dataclass(frozen=True)
class SessionConfig:
    region: str
    system_node_vm_size: str
    system_node_count: int
    gpu_node_pool_enabled: bool
    gpu_node_vm_size: str
    gpu_node_count: int
    os_disk_size_gb: int
    kubernetes_version: str

    def __post_init__(self) -> None:
        if self.system_node_vm_size not in ALLOWED_SYSTEM_SKUS:
            raise G3Error(f"system SKU {self.system_node_vm_size} is not in the reviewed allowlist")
        if self.gpu_node_vm_size not in ALLOWED_GPU_SKUS:
            raise G3Error(f"GPU SKU {self.gpu_node_vm_size} is not in the reviewed allowlist")
        if self.system_node_count not in (1, 2):
            raise G3Error("system_node_count must be 1 or 2")
        if self.gpu_node_count != 1:
            raise G3Error("gpu_node_count must be exactly 1")
        if self.os_disk_size_gb != 64:
            raise G3Error("os_disk_size_gb must be 64 (the P6 band the evidence prices)")

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.__dict__)


def config_from_dict(raw: dict[str, Any]) -> SessionConfig:
    if raw.get("schema_version") != "1":
        raise G3Error("unsupported session config schema_version")
    return SessionConfig(
        region=str(raw["region"]),
        system_node_vm_size=str(raw["system_node_vm_size"]),
        system_node_count=int(raw["system_node_count"]),
        gpu_node_pool_enabled=bool(raw["gpu_node_pool_enabled"]),
        gpu_node_vm_size=str(raw["gpu_node_vm_size"]),
        gpu_node_count=int(raw["gpu_node_count"]),
        os_disk_size_gb=int(raw["os_disk_size_gb"]),
        kubernetes_version=str(raw["kubernetes_version"]),
    )


@dataclass(frozen=True)
class LineItem:
    key: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    amount: Decimal
    basis: str


@dataclass(frozen=True)
class Estimate:
    billable_hours: int
    lines: list[LineItem] = field(default_factory=list)
    subtotal: Decimal = ZERO
    fx_allowance: Decimal = ZERO
    tax_allowance: Decimal = ZERO
    reservation_usd: Decimal = ZERO
    config_hash: str = ""
    evidence_hash: str = ""
    policy_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "billable_hours": self.billable_hours,
            "lines": [line.__dict__ for line in self.lines],
            "subtotal_usd": self.subtotal,
            "fx_allowance_usd": self.fx_allowance,
            "tax_allowance_usd": self.tax_allowance,
            "reservation_usd": self.reservation_usd,
            "config_hash": self.config_hash,
            "evidence_hash": self.evidence_hash,
            "policy_hash": self.policy_hash,
            "note": (
                "Retail-price estimate with explicit allowances; "
                "not a guaranteed charge or a billing cap."
            ),
        }


def billable_hours(policy: BudgetPolicy) -> int:
    """Whole billable hours: the session target plus every allowance, rounded up."""
    return math.ceil(policy.total_session_minutes / 60)


def estimate(config: SessionConfig, policy: BudgetPolicy, evidence: dict[str, Any]) -> Estimate:
    if evidence.get("currency") != policy.pricing_currency:
        raise G3Error(
            f"evidence currency {evidence.get('currency')!r} is not the pricing currency "
            f"{policy.pricing_currency!r}; no silent conversion"
        )
    if evidence.get("region") != config.region:
        raise G3Error("price evidence is for a different region than the session")
    if config.gpu_node_pool_enabled and config.gpu_node_count > policy.max_gpu_nodes:
        raise G3Error("session asks for more GPU nodes than the policy allows")

    hours = Decimal(billable_hours(policy))
    lines: list[LineItem] = []

    def add(key: str, quantity: Decimal, unit: str, price_key: str, basis: str) -> None:
        price = unit_price(evidence, price_key)
        lines.append(LineItem(key, quantity, unit, price, quantity * price, basis))

    cpu_nodes = Decimal(config.system_node_count)
    add(
        "cpu_compute",
        cpu_nodes * hours,
        "node-hour",
        "vm_cpu_hour",
        f"{config.system_node_vm_size} x {config.system_node_count}",
    )
    disks = cpu_nodes
    if config.gpu_node_pool_enabled:
        gpu_nodes = Decimal(config.gpu_node_count)
        add(
            "gpu_compute",
            gpu_nodes * hours,
            "node-hour",
            "vm_gpu_hour",
            f"{config.gpu_node_vm_size} x {config.gpu_node_count}",
        )
        disks += gpu_nodes
    # Monthly disk price spread over 730 hours, rounded UP at six decimals.
    disk_hour_price = (unit_price(evidence, "os_disk_p6_month") / HOURS_PER_MONTH).quantize(
        MICRO, rounding=ROUND_UP
    )
    lines.append(
        LineItem(
            "os_disks",
            disks * hours,
            "disk-hour",
            disk_hour_price,
            disks * hours * disk_hour_price,
            "P6 64 GB per node, monthly price / 730",
        )
    )
    add("load_balancer_rules", hours, "hour", "lb_rules_hour", "AKS Standard LB, first 5 rules")
    add(
        "load_balancer_data",
        policy.load_balancer_data_gb_per_session,
        "GB",
        "lb_data_gb",
        "policy bound",
    )
    add("public_ip", hours, "hour", "public_ip_hour", "one managed outbound IP")
    add(
        "egress",
        policy.egress_gb_per_session,
        "GB",
        "egress_gb",
        "policy bound, dearest tier, no free allowance",
    )
    add(
        "registry",
        Decimal(policy.registry_days_per_session),
        "day",
        "acr_basic_day",
        "Basic registry, whole days",
    )
    acr_storage_month = unit_price(evidence, "acr_storage_gb_month")
    lines.append(
        LineItem(
            "registry_storage",
            policy.registry_storage_gb,
            "GB-month",
            acr_storage_month,
            policy.registry_storage_gb * acr_storage_month,
            "whole month reserved, conservative",
        )
    )
    add(
        "watchdog",
        Decimal(policy.watchdog_minutes_per_session),
        "minute",
        "automation_minute",
        "no free minutes assumed",
    )
    blob_month = unit_price(evidence, "blob_hot_gb_month")
    retained_gb_months = policy.retained_storage_gb * Decimal(policy.retained_months_reserved)
    lines.append(
        LineItem(
            "retained_storage",
            retained_gb_months,
            "GB-month",
            blob_month,
            retained_gb_months * blob_month,
            "retained inputs/models reserve",
        )
    )

    subtotal = sum((line.amount for line in lines), ZERO)
    fx = (
        subtotal * policy.fx_allowance_ratio
        if policy.billing_currency != policy.pricing_currency
        else ZERO
    )
    tax = subtotal * policy.tax_allowance_ratio
    total = round_up_cents(subtotal + fx + tax)
    return Estimate(
        billable_hours=int(hours),
        lines=lines,
        subtotal=subtotal,
        fx_allowance=fx,
        tax_allowance=tax,
        reservation_usd=total,
        config_hash=config.config_hash,
        evidence_hash=str(evidence["evidence_hash"]),
        policy_hash=policy.policy_hash,
    )


def format_estimate(est: Estimate) -> str:
    rows = [f"billable hours (target + allowances, rounded up): {est.billable_hours}"]
    for line in est.lines:
        amount = round_up_cents(line.amount)
        rows.append(
            f"  {line.key:22s} {line.quantity:>10} {line.unit:10s} x {line.unit_price:>12}"
            f" = {amount:>8}  ({line.basis})"
        )
    pad = " " * 54
    rows.append(f"  subtotal{pad}{round_up_cents(est.subtotal):>8}")
    rows.append(f"  fx allowance{pad[4:]}{round_up_cents(est.fx_allowance):>8}")
    rows.append(f"  tax allowance{pad[5:]}{round_up_cents(est.tax_allowance):>8}")
    rows.append(f"  RESERVATION (USD, rounded up){pad[21:]}{est.reservation_usd:>8}")
    return "\n".join(rows)
