"""Session admission: pure decision logic, no I/O, no bypass flag.

Two separate answers are always produced:
  cost_fits                 the proposed reservation fits under the admission
                            limit given settled, unresolved and retained exposure;
  cloud_execution_allowed   every operational control is also satisfied
                            (fresh evidence, verified quota and permissions,
                            no live session, watchdog live-verified, ...).
A dry run may say "cost fits" while cloud execution is blocked; that is the
expected G3 result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from discover import check_discovery
from estimate import Estimate
from g3lib import ZERO, parse_utc
from ledger import Exposure
from policy import BudgetPolicy
from prices import check_evidence


@dataclass(frozen=True)
class WatchdogVerification:
    live_verified: bool
    verified_utc: str | None = None
    mode_verified: str | None = None
    evidence_ref: str | None = None


@dataclass(frozen=True)
class AdmissionDecision:
    cost_fits: bool
    cloud_execution_allowed: bool
    projected_total_usd: Decimal
    headroom_usd: Decimal
    cost_reasons: list[str] = field(default_factory=list)
    control_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_fits": self.cost_fits,
            "cloud_execution_allowed": self.cloud_execution_allowed,
            "projected_total_usd": self.projected_total_usd,
            "headroom_usd": self.headroom_usd,
            "cost_reasons": self.cost_reasons,
            "control_reasons": self.control_reasons,
        }


def decide(
    policy: BudgetPolicy,
    exposure: Exposure,
    estimate: Estimate,
    evidence: dict[str, Any],
    discovery: dict[str, Any],
    watchdog: WatchdogVerification,
    now: datetime,
    subscription_id: str,
    region: str,
    bound_config_hash: str | None = None,
) -> AdmissionDecision:
    cost_reasons: list[str] = []
    control_reasons: list[str] = []

    projected = exposure.committed_usd + estimate.reservation_usd
    headroom = policy.admission_limit_usd - projected
    if projected > policy.admission_limit_usd:
        cost_reasons.append(
            f"projected exposure {projected} USD exceeds admission limit "
            f"{policy.admission_limit_usd} USD (settled {exposure.settled_usd}, "
            f"unresolved {exposure.unresolved_usd}, retained {exposure.retained_reserve_usd}, "
            f"reservation {estimate.reservation_usd}); the {policy.contingency_usd} USD "
            "contingency is not available to ordinary admission"
        )
    if estimate.reservation_usd <= ZERO:
        cost_reasons.append(
            "reservation must be positive; a zero estimate means something is unpriced"
        )
    cost_fits = not cost_reasons

    if estimate.policy_hash != policy.policy_hash:
        control_reasons.append("estimate was made under a different policy version")
    if bound_config_hash is not None and bound_config_hash != estimate.config_hash:
        control_reasons.append("configuration changed since the estimate; re-estimate and re-admit")
    if estimate.evidence_hash != evidence.get("evidence_hash"):
        control_reasons.append("estimate does not match the current price evidence")
    control_reasons.extend(
        check_evidence(evidence, now, policy.max_price_evidence_age_hours, region)
    )
    control_reasons.extend(
        check_discovery(discovery, now, policy.max_discovery_age_hours, subscription_id, region)
    )
    evaluation = discovery.get("evaluation", {})
    control_reasons.extend(f"discovery blocker: {b}" for b in evaluation.get("blockers", []))
    if not evaluation.get("quota_verified"):
        control_reasons.append("required quota is not verified")
    if not evaluation.get("permissions_verified"):
        control_reasons.append("caller permissions are not verified")
    if exposure.live_sessions:
        control_reasons.append(f"another session is live: {exposure.live_sessions}")
    if exposure.unverified_teardowns:
        control_reasons.append(
            f"unresolved live resources / unverified teardown: {exposure.unverified_teardowns}"
        )
    if not watchdog.live_verified:
        control_reasons.append("cloud execution blocked: watchdog not live-verified")
    elif watchdog.verified_utc is None:
        control_reasons.append("watchdog verification has no timestamp")
    else:
        age = now - parse_utc(watchdog.verified_utc)
        if age > timedelta(days=policy.max_watchdog_verification_age_days):
            control_reasons.append(
                f"watchdog verification is {age.days} days old; "
                f"maximum {policy.max_watchdog_verification_age_days}"
            )
    if not cost_fits:
        control_reasons.append("cost does not fit")

    return AdmissionDecision(
        cost_fits=cost_fits,
        cloud_execution_allowed=not control_reasons,
        projected_total_usd=projected,
        headroom_usd=headroom,
        cost_reasons=cost_reasons,
        control_reasons=control_reasons,
    )
