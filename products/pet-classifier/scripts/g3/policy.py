"""The versioned budget policy: numbers the admission logic enforces.

Loaded from ``deploy/g3/budget-policy.v1.json`` (committed, reviewed) and
mirrored by the Terraform ``budget_policy`` variable so both agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from g3lib import POLICY_FILE, G3Error, canonical_hash, money, read_json


@dataclass(frozen=True)
class BudgetPolicy:
    policy_version: str
    project: str
    project_limit_usd: Decimal
    admission_limit_usd: Decimal
    contingency_usd: Decimal
    max_concurrent_sessions: int
    max_gpu_nodes: int
    active_target_minutes: int
    training_job_deadline_seconds: int
    provisioning_minutes: int
    image_pull_minutes: int
    deletion_delay_minutes: int
    watchdog_delay_minutes: int
    egress_gb_per_session: Decimal
    load_balancer_data_gb_per_session: Decimal
    registry_days_per_session: int
    registry_storage_gb: Decimal
    watchdog_minutes_per_session: int
    retained_storage_gb: Decimal
    retained_months_reserved: int
    retained_blob_expiry_days: int
    max_price_evidence_age_hours: int
    max_discovery_age_hours: int
    max_watchdog_verification_age_days: int
    pricing_currency: str
    billing_currency: str
    fx_allowance_ratio: Decimal
    tax_allowance_ratio: Decimal
    policy_hash: str

    @property
    def total_session_minutes(self) -> int:
        """Active target plus every explicit allowance. A planning figure only."""
        return (
            self.active_target_minutes
            + self.provisioning_minutes
            + self.image_pull_minutes
            + self.deletion_delay_minutes
            + self.watchdog_delay_minutes
        )

    @property
    def expiry_minutes_after_reservation(self) -> int:
        """Expiry from the reservation: active target + provisioning + pull + deletion delay.

        The watchdog delay is NOT included: it describes how late the watchdog
        may act after expiry and is charged for, but it never pushes the
        deadline itself later.
        """
        return (
            self.active_target_minutes
            + self.provisioning_minutes
            + self.image_pull_minutes
            + self.deletion_delay_minutes
        )


def _validate(raw: dict[str, Any], policy: BudgetPolicy) -> None:
    if policy.admission_limit_usd + policy.contingency_usd != policy.project_limit_usd:
        raise G3Error("admission_limit_usd + contingency_usd must equal project_limit_usd")
    if policy.project_limit_usd != money("100") or policy.admission_limit_usd != money("60"):
        raise G3Error("policy v1 fixes project_limit_usd=100 and admission_limit_usd=60")
    if policy.max_concurrent_sessions != 1 or policy.max_gpu_nodes != 1:
        raise G3Error("policy v1 allows at most one concurrent session and one GPU node")
    if not 0 < policy.active_target_minutes <= 120:
        raise G3Error("active_target_minutes must be within (0, 120]")
    if not 0 < policy.training_job_deadline_seconds <= 1800:
        raise G3Error("training_job_deadline_seconds must be within (0, 1800]")
    for name in (
        "provisioning_minutes",
        "image_pull_minutes",
        "deletion_delay_minutes",
        "watchdog_delay_minutes",
    ):
        if getattr(policy, name) <= 0:
            raise G3Error(f"{name} must be a positive explicit allowance")
    if policy.watchdog_delay_minutes < 60:
        raise G3Error("watchdog_delay_minutes must cover the hourly sweep cadence (>= 60)")
    if policy.pricing_currency != "USD":
        raise G3Error("retail evidence and reservations are USD")
    if policy.billing_currency != policy.pricing_currency and policy.fx_allowance_ratio <= 0:
        raise G3Error("a billing currency other than USD needs a positive fx_allowance_ratio")
    if policy.tax_allowance_ratio < 0 or policy.fx_allowance_ratio < 0:
        raise G3Error("allowance ratios cannot be negative")
    if raw.get("schema_version") != "1":
        raise G3Error("unsupported policy schema_version")


def policy_from_dict(raw: dict[str, Any]) -> BudgetPolicy:
    limits, session, bounds = raw["limits"], raw["session"], raw["bounds"]
    allowance, retention, evidence, currency = (
        session["allowance_minutes"],
        raw["retention"],
        raw["evidence"],
        raw["currency"],
    )
    policy = BudgetPolicy(
        policy_version=str(raw["policy_version"]),
        project=str(raw["project"]),
        project_limit_usd=money(limits["project_limit_usd"]),
        admission_limit_usd=money(limits["admission_limit_usd"]),
        contingency_usd=money(limits["contingency_usd"]),
        max_concurrent_sessions=int(limits["max_concurrent_sessions"]),
        max_gpu_nodes=int(limits["max_gpu_nodes"]),
        active_target_minutes=int(session["active_target_minutes"]),
        training_job_deadline_seconds=int(session["training_job_deadline_seconds"]),
        provisioning_minutes=int(allowance["provisioning"]),
        image_pull_minutes=int(allowance["image_pull"]),
        deletion_delay_minutes=int(allowance["deletion_delay"]),
        watchdog_delay_minutes=int(allowance["watchdog_delay"]),
        egress_gb_per_session=money(bounds["egress_gb_per_session"]),
        load_balancer_data_gb_per_session=money(bounds["load_balancer_data_gb_per_session"]),
        registry_days_per_session=int(bounds["registry_days_per_session"]),
        registry_storage_gb=money(bounds["registry_storage_gb"]),
        watchdog_minutes_per_session=int(bounds["watchdog_minutes_per_session"]),
        retained_storage_gb=money(bounds["retained_storage_gb"]),
        retained_months_reserved=int(bounds["retained_months_reserved"]),
        retained_blob_expiry_days=int(retention["retained_blob_expiry_days"]),
        max_price_evidence_age_hours=int(evidence["max_price_evidence_age_hours"]),
        max_discovery_age_hours=int(evidence["max_discovery_age_hours"]),
        max_watchdog_verification_age_days=int(evidence["max_watchdog_verification_age_days"]),
        pricing_currency=str(currency["pricing_currency"]),
        billing_currency=str(currency["billing_currency"]),
        fx_allowance_ratio=money(currency["fx_allowance_ratio"]),
        tax_allowance_ratio=money(currency["tax_allowance_ratio"]),
        policy_hash=canonical_hash(raw),
    )
    _validate(raw, policy)
    return policy


def load_policy(path: Path = POLICY_FILE) -> BudgetPolicy:
    return policy_from_dict(read_json(path))
