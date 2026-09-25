"""The lifetime project cost ledger.

Cumulative, never reset monthly. One JSON file under ``.local/g3/ledger``
(private, git-ignored, on the operator's machine so it survives every
cluster teardown), written atomically under a local lock. Session ids are
immutable; every mutation is idempotent so a retried command cannot count a
session, a teardown or a reconciliation twice.

Exposure rules:
  - a session that is not settled contributes max(reservation, actual so far);
  - a session is settled only when its actual charges are reported with
    complete coverage AND its teardown is verified; then it contributes the
    actual amount (which may exceed the reservation);
  - an empty or delayed billing response is not zero cost: coverage stays
    incomplete and the reservation stays;
  - retained storage carries its own standing reserve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from g3lib import (
    LEDGER_FILE,
    LEDGER_LOCK,
    ZERO,
    G3Error,
    atomic_write_json,
    file_lock,
    format_utc,
    money,
    read_json,
    validate_session_id,
)

LIVE_STATUSES = ("reserved", "provisioning", "active", "ended")
SETTLED = "settled"


@dataclass(frozen=True)
class Exposure:
    settled_usd: Decimal
    unresolved_usd: Decimal
    retained_reserve_usd: Decimal
    live_sessions: list[str]
    unverified_teardowns: list[str]

    @property
    def committed_usd(self) -> Decimal:
        return self.settled_usd + self.unresolved_usd + self.retained_reserve_usd


def empty_ledger(policy_version: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "policy_version": policy_version,
        "retained_reserve_usd": "0",
        "sessions": {},
    }


def session_exposure(record: dict[str, Any]) -> tuple[Decimal, bool]:
    """(amount, settled). Never releases a reservation while anything is unresolved."""
    reserved = money(record["reserved_usd"])
    actual = money(record.get("actual_usd", "0"))
    coverage_complete = bool(record.get("actual_coverage", {}).get("complete", False))
    teardown_verified = bool(record.get("teardown", {}).get("verified", False))
    settled = record["status"] == SETTLED and coverage_complete and teardown_verified
    if settled:
        return actual, True
    return max(reserved, actual), False


def exposure(ledger: dict[str, Any]) -> Exposure:
    settled, unresolved = ZERO, ZERO
    live: list[str] = []
    unverified: list[str] = []
    for session_id, record in sorted(ledger["sessions"].items()):
        if record["status"] == "cancelled":
            continue
        amount, is_settled = session_exposure(record)
        if is_settled:
            settled += amount
        else:
            unresolved += amount
        if record["status"] in LIVE_STATUSES:
            live.append(session_id)
        if record["status"] not in ("cancelled",) and not record.get("teardown", {}).get(
            "verified"
        ):
            unverified.append(session_id)
    return Exposure(
        settled, unresolved, money(ledger.get("retained_reserve_usd", "0")), live, unverified
    )


def reserve(
    ledger: dict[str, Any],
    session_id: str,
    now: datetime,
    expiry: datetime,
    reserved_usd: Decimal,
    config_hash: str,
    evidence_hash: str,
    policy_hash: str,
    resource_groups: list[str],
) -> dict[str, Any]:
    """Add a reservation. Repeating the identical call is a no-op; a different one is refused."""
    validate_session_id(session_id)
    record = {
        "session_id": session_id,
        "status": "reserved",
        "reserved_utc": format_utc(now),
        "expiry_utc": format_utc(expiry),
        "config_hash": config_hash,
        "evidence_hash": evidence_hash,
        "policy_hash": policy_hash,
        "reserved_usd": str(reserved_usd),
        "estimated_unbilled_usd": str(reserved_usd),
        "actual_usd": "0",
        "actual_coverage": {
            "complete": False,
            "from_utc": None,
            "to_utc": None,
            "source": None,
            "reported_utc": None,
        },
        "resource_ids": [],
        "resource_groups": resource_groups,
        "teardown": {"verified": False, "verified_utc": None, "residuals": []},
        "reconciliation": {"status": "pending", "events": []},
    }
    existing = ledger["sessions"].get(session_id)
    if existing is not None:
        comparable = {k: v for k, v in existing.items() if k in record and k not in ("status",)}
        wanted = {k: v for k, v in record.items() if k in comparable}
        if comparable != wanted:
            raise G3Error(f"session {session_id} already reserved with different content")
        return ledger
    for other_id, other in ledger["sessions"].items():
        if other["status"] in LIVE_STATUSES:
            raise G3Error(f"session {other_id} is still {other['status']}; one session at a time")
    ledger["sessions"][session_id] = record
    return ledger


def set_status(ledger: dict[str, Any], session_id: str, status: str) -> dict[str, Any]:
    if status not in (*LIVE_STATUSES, "cancelled"):
        raise G3Error(f"unknown status {status!r}")
    record = _record(ledger, session_id)
    if status == "cancelled" and record.get("resource_ids"):
        raise G3Error(
            "a session with recorded resources cannot be cancelled; verify teardown instead"
        )
    record["status"] = status
    return ledger


def record_resources(
    ledger: dict[str, Any], session_id: str, resource_ids: list[str]
) -> dict[str, Any]:
    record = _record(ledger, session_id)
    record["resource_ids"] = sorted(set(record["resource_ids"]) | set(resource_ids))
    if record["status"] == "reserved":
        record["status"] = "provisioning"
    return ledger


def record_teardown(
    ledger: dict[str, Any], session_id: str, now: datetime, residuals: list[str], result_ref: str
) -> dict[str, Any]:
    """Teardown is verified only when the re-inventory found nothing. Idempotent."""
    record = _record(ledger, session_id)
    event = {"kind": "teardown", "ref": result_ref, "residuals": sorted(residuals)}
    if event in record["reconciliation"]["events"]:
        return ledger
    record["reconciliation"]["events"].append(event)
    if residuals:
        record["teardown"] = {
            "verified": False,
            "verified_utc": None,
            "residuals": sorted(residuals),
        }
        record["status"] = "ended"
    else:
        record["teardown"] = {"verified": True, "verified_utc": format_utc(now), "residuals": []}
        record["status"] = "ended"
    return ledger


def reconcile(
    ledger: dict[str, Any],
    session_id: str,
    now: datetime,
    actual_usd: Decimal,
    coverage_from: datetime,
    coverage_to: datetime,
    complete: bool,
    source: str,
) -> dict[str, Any]:
    """Record reported charges for a coverage window. Idempotent per window+source.

    Later reports REPLACE earlier ones for the same session (billing data is
    cumulative for the window), so overlapping data is never summed twice.
    """
    record = _record(ledger, session_id)
    if actual_usd < 0:
        raise G3Error("actual charges cannot be negative")
    if coverage_to <= coverage_from:
        raise G3Error("coverage window must be non-empty")
    if complete and coverage_from > record_reserved_at(record):
        raise G3Error("complete coverage must start at or before the reservation")
    event = {
        "kind": "reconcile",
        "source": source,
        "from_utc": format_utc(coverage_from),
        "to_utc": format_utc(coverage_to),
        "actual_usd": str(actual_usd),
        "complete": complete,
    }
    if event in record["reconciliation"]["events"]:
        return ledger
    record["reconciliation"]["events"].append(event)
    record["actual_usd"] = str(actual_usd)
    record["actual_coverage"] = {
        "complete": complete,
        "from_utc": event["from_utc"],
        "to_utc": event["to_utc"],
        "source": source,
        "reported_utc": format_utc(now),
    }
    reserved = money(record["reserved_usd"])
    record["estimated_unbilled_usd"] = (
        str(max(reserved - actual_usd, ZERO)) if not complete else "0"
    )
    if complete and record["teardown"]["verified"]:
        record["status"] = SETTLED
        record["reconciliation"]["status"] = SETTLED
    else:
        record["reconciliation"]["status"] = "partial"
    return ledger


def record_reserved_at(record: dict[str, Any]) -> datetime:
    from g3lib import parse_utc

    return parse_utc(record["reserved_utc"])


def _record(ledger: dict[str, Any], session_id: str) -> dict[str, Any]:
    validate_session_id(session_id)
    record = ledger["sessions"].get(session_id)
    if not isinstance(record, dict):
        raise G3Error(f"session {session_id} is not in the ledger")
    return record


class LedgerStore:
    """Load/save with the local lock and atomic writes."""

    def __init__(self, path: Path = LEDGER_FILE, lock_path: Path = LEDGER_LOCK) -> None:
        self.path = path
        self.lock_path = lock_path

    def load(self, policy_version: str) -> dict[str, Any]:
        if not self.path.is_file():
            return empty_ledger(policy_version)
        ledger = read_json(self.path)
        if ledger.get("schema_version") != "1":
            raise G3Error("unsupported ledger schema")
        return ledger

    def save(self, ledger: dict[str, Any]) -> None:
        atomic_write_json(self.path, ledger)

    def locked(self) -> Any:
        return file_lock(self.lock_path)
